#!/usr/bin/env python3
"""手动安全更新 Hermes Agent Docker 容器。

支持两种模式：
1. Docker Compose 模式：容器由 compose 管理（例如 1Panel 安装的应用）。
2. 普通 Docker 模式：容器由 docker run 直接创建。

升级前会自动备份镜像和配置；升级失败可自动回滚。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def run(cmd, check=True, capture=False, **kwargs):
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    if capture:
        return subprocess.run(cmd, check=check, capture_output=True, text=True, **kwargs)
    return subprocess.run(cmd, check=check, **kwargs)


def docker_inspect(ref):
    try:
        out = run(["docker", "inspect", ref], capture=True)
        return json.loads(out.stdout)
    except subprocess.CalledProcessError:
        return None


def get_current_image_from_compose(compose_file: Path):
    text = compose_file.read_text(encoding="utf-8")
    m = re.search(r'^\s*image:\s*(\S+)', text, re.MULTILINE)
    if not m:
        raise SystemExit(f"在 {compose_file} 中找不到 image 行")
    return m.group(1)


def set_compose_image(compose_file: Path, new_image: str):
    text = compose_file.read_text(encoding="utf-8")
    text = re.sub(r'^(\s*image:\s*)\S+', r'\g<1>' + new_image, text, flags=re.MULTILINE)
    compose_file.write_text(text, encoding="utf-8")


def compose_update(compose_file: Path, project_dir: Path, project_name: str, target_image: str, yes: bool, dry_run: bool):
    current_image = get_current_image_from_compose(compose_file)
    print(f"Compose 文件: {compose_file}")
    print(f"当前镜像: {current_image}")
    print(f"目标镜像: {target_image}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = Path("/opt/hermes-hub/backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    compose_backup = backup_dir / f"docker-compose-{timestamp}.yml"
    env_backup = backup_dir / f".env-{timestamp}"

    shutil.copy2(compose_file, compose_backup)
    env_file = project_dir / ".env"
    if env_file.exists():
        shutil.copy2(env_file, env_backup)

    # 备份当前镜像
    backup_tag = f"{current_image.rsplit(':', 1)[0] if ':' in current_image else current_image}:backup-{timestamp}"
    run(["docker", "tag", current_image, backup_tag], check=False)
    print(f"镜像已备份: {backup_tag}")

    if dry_run:
        print("[dry-run] 将修改 compose 镜像并执行 docker compose up -d")
        return

    if not yes:
        ans = input("确认升级? [y/N] ")
        if ans.lower() not in ("y", "yes"):
            print("已取消")
            return

    set_compose_image(compose_file, target_image)
    try:
        run(["docker", "compose", "-f", str(compose_file), "-p", project_name, "pull"])
        run(["docker", "compose", "-f", str(compose_file), "-p", project_name, "up", "-d"])
        container_name = get_container_name_from_env(env_file) if env_file.exists() else None
        if not container_name:
            # fallback: try to find any container in project
            out = run(["docker", "compose", "-f", str(compose_file), "-p", project_name, "ps", "-q"], capture=True)
            container_name = out.stdout.strip().splitlines()[0] if out.stdout.strip() else None
        verify(container_name)
        print("升级成功")
    except Exception as e:
        print(f"升级失败: {e}")
        print("执行回滚...")
        shutil.copy2(compose_backup, compose_file)
        if env_file.exists():
            shutil.copy2(env_backup, env_file)
        run(["docker", "compose", "-f", str(compose_file), "-p", project_name, "up", "-d"])
        print("已回滚到原镜像")
        raise


def docker_run_update(container_name: str, target_image: str | None, yes: bool, dry_run: bool):
    info = docker_inspect(container_name)
    if not info:
        raise SystemExit(f"找不到容器: {container_name}")
    c = info[0]
    current_image = c["Config"]["Image"]
    if not target_image:
        repo = current_image.rsplit(":", 1)[0] if ":" in current_image else current_image
        target_image = f"{repo}:latest"

    print(f"容器: {container_name}")
    print(f"当前镜像: {current_image}")
    print(f"目标镜像: {target_image}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_tag = f"{current_image.rsplit(':', 1)[0] if ':' in current_image else current_image}:backup-{timestamp}"
    run(["docker", "tag", current_image, backup_tag], check=False)
    print(f"镜像已备份: {backup_tag}")

    if dry_run:
        cmd = build_docker_run_command(c, target_image, container_name)
        print("[dry-run] 将执行以下命令创建新容器:")
        print(" ".join(shlex.quote(str(x)) for x in cmd))
        return

    if not yes:
        ans = input("确认升级? [y/N] ")
        if ans.lower() not in ("y", "yes"):
            print("已取消")
            return

    run(["docker", "pull", target_image])
    run(["docker", "stop", container_name])
    run(["docker", "rename", container_name, f"{container_name}-backup-{timestamp}"])
    try:
        cmd = build_docker_run_command(c, target_image, container_name)
        run(cmd)
        verify(container_name)
        print("升级成功")
    except Exception as e:
        print(f"升级失败: {e}")
        print("执行回滚...")
        run(["docker", "stop", container_name], check=False)
        run(["docker", "rm", container_name], check=False)
        run(["docker", "rename", f"{container_name}-backup-{timestamp}", container_name])
        run(["docker", "start", container_name])
        print("已回滚")
        raise


def build_docker_run_command(c: dict, image: str, name: str):
    import shlex
    hc = c.get("HostConfig", {})
    cfg = c.get("Config", {})
    cmd = ["docker", "run", "-d", "--name", name]

    network = hc.get("NetworkMode")
    if network and network != "default":
        cmd.extend(["--network", network])

    ports = hc.get("PortBindings") or {}
    for container_port, bindings in ports.items():
        for b in bindings or []:
            host_ip = b.get("HostIp", "")
            host_port = b.get("HostPort", "")
            mapping = container_port
            if host_port:
                if host_ip:
                    mapping = f"{host_ip}:{host_port}:{container_port}"
                else:
                    mapping = f"{host_port}:{container_port}"
            cmd.extend(["-p", mapping])

    binds = hc.get("Binds") or []
    for b in binds:
        cmd.extend(["-v", b])

    env = cfg.get("Env") or []
    for e in env:
        cmd.extend(["-e", e])

    restart = hc.get("RestartPolicy", {}).get("Name")
    if restart:
        cmd.extend(["--restart", restart])

    cmd.append(image)
    container_cmd = cfg.get("Cmd") or []
    cmd.extend(container_cmd)
    return cmd


def get_container_name_from_env(env_file: Path):
    if not env_file.exists():
        return None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("CONTAINER_NAME="):
            v = line.split("=", 1)[1].strip()
            return v.strip("'").strip('"')
    return None


def verify(container_name_or_id):
    if not container_name_or_id:
        print("无法自动验证：未找到容器名")
        return
    print("等待容器启动...")
    import time
    for i in range(10):
        time.sleep(2)
        out = run(["docker", "exec", container_name_or_id, "hermes", "--version"], check=False, capture=True)
        if out.returncode == 0:
            print(f"Hermes 验证通过: {out.stdout.strip()}")
            return
        print(f"  重试 {i+1}/10...")
    raise RuntimeError("Hermes 验证失败")


def main():
    parser = argparse.ArgumentParser(description="安全更新 Hermes Agent 容器")
    parser.add_argument("container", nargs="?", default=os.environ.get("HERMES_CONTAINER", "hermes"), help="容器名")
    parser.add_argument("--target-image", help="目标镜像，默认使用当前镜像仓库的 :latest")
    parser.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    parser.add_argument("--dry-run", action="store_true", help="仅打印将要执行的操作")
    args = parser.parse_args()

    container_name = args.container
    info = docker_inspect(container_name)
    if not info:
        raise SystemExit(f"找不到容器: {container_name}")

    labels = info[0].get("Config", {}).get("Labels", {}) or {}
    compose_file = labels.get("com.docker.compose.project.config_files")
    project_dir = labels.get("com.docker.compose.project.working_dir")
    project_name = labels.get("com.docker.compose.project") or (Path(project_dir).name.lower() if project_dir else None)

    if compose_file and project_dir:
        compose_update(
            Path(compose_file),
            Path(project_dir),
            project_name,
            args.target_image or f"{info[0]['Config']['Image'].rsplit(':', 1)[0]}:latest",
            args.yes,
            args.dry_run,
        )
    else:
        docker_run_update(container_name, args.target_image, args.yes, args.dry_run)


if __name__ == "__main__":
    main()
