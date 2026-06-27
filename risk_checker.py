"""
风险等级分类器
判断 Hermes 操作请求的风险等级，实现智能审批
"""

import re
from typing import Optional

# ── 高风险关键词 ──────────────────────────────────
# 这些操作通常不可逆，大概率需要用户确认
HIGH_RISK_PATTERNS = [
    r"\brm\s+-rf\b", r"\brm\s+-r\b", r"\brmdir\b",
    r"\bdel(ete)?\s+/", r"\bformat\b", r"\bmkfs\b",
    r"\bdd\s+if=", r"\bshred\b", r"\bwipe\b",
    r"\bsudo\b", r"\bchmod\s+777\b", r"\bchown\b",
    r"\bgit\s+push\s+--force\b", r"\bgit\s+push\s+-f\b",
    r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-f[fd]*\b",
    r"\bdrop\s+table\b", r"\bdrop\s+database\b",
    r"\btruncate\b", r"\bDROP\s+TABLE\b",
    r"\bnpm\s+publish\b", r"\bnpm\s+unpublish\b",
    r"\bpip\s+uninstall\b", r"\buninstall\b",
    r"\byarn\s+publish\b",
    r"\breboot\b", r"\bshutdown\b", r"\bpoweroff\b",
    r"\bdocker\s+rm\s+-f\b", r"\bdocker\s+system\s+prune\b",
    r"\bkubectl\s+delete\b",
    # token/secret/password keywords alone cause too many false positives;
    r"\bkill\s+-9\b",
    r"rm\s+-rf\s+[/~]",  # 系统级删除
]

# ── 中风险关键词 ──────────────────────────────────
# 这些操作有影响但可逆或风险较低
MEDIUM_RISK_PATTERNS = [
    r"\bgit\s+push\b", r"\bgit\s+commit\b", r"\bgit\s+merge\b",
    r"\bgit\s+rebase\b", r"\bgit\s+checkout\b",
    r"\bgit\s+fetch\b", r"\bgit\s+pull\b",
    r"\bnpm\s+install\b", r"\bpip\s+install\b",
    r"\byarn\s+add\b", r"\byarn\s+install\b",
    r"\bbrew\s+install\b", r"\bapt\s+install\b",
    r"\bpacman\s+-S\b", r"\bdnf\s+install\b",
    r"\bcargo\s+install\b", r"\bgo\s+install\b",
    r"\bdocker\s+build\b", r"\bdocker\s+push\b",
    r"\bdocker\s+compose\b",
    r"\bdeploy\b", r"\brelease\b",
    r"\bchmod\b", r"\bchown\b",
    r"\bmv\b", r"\bcp\b",  # 移动/复制可能覆盖文件
    r"\bmkdir\b", r"\btouch\b", r"\becho\s+>", r"\becho\s+>>",
    r"\bsed\s+-i\b", r"\binplace\b",
    r"\bwget\b", r"\bcurl\s+-o\b", r"\bcurl\s+--output\b",
    r"\bdig\b", r"\bnslookup\b",
    r"\bnmap\b", r"\bnetcat\b", r"\bnc\b",
    r"\bssh\b", r"\bscp\b", r"\brsync\b",
    r"\bkill\b",
    r"\bservice\s+restart\b", r"\bsystemctl\s+restart\b",
    r"\bset\s+-e\b", r"\bexport\s+",
    r"\b>[\s\w]", r"\b>>[\s\w]",  # 输出重定向
    r"\bvenv\b", r"\bvirtualenv\b",
    r"\bconda\s+install\b",
]

# ── 安全操作 ──────────────────────────────────────
# 这些操作通常无害，自动放行
LOW_RISK_PATTERNS = [
    r"\bls\b", r"\bcat\b", r"\bhead\b", r"\btail\b",
    r"\bgrep\b", r"\begrep\b", r"\bfgrep\b",
    r"\bfind\b", r"\blocate\b",
    r"\bwc\b", r"\bsort\b", r"\buniq\b",
    r"\bless\b", r"\bmore\b",
    r"\bpwd\b", r"\bwhich\b", r"\bwhere\b",
    r"\bwhoami\b", r"\bid\b",
    r"\bdate\b", r"\bcal\b",
    r"\bdf\b", r"\bdu\b",
    r"\bpython\s+\S+\.py\b", r"\bpython3\s+\S+\.py\b",
    r"\bnpm\s+run\b", r"\byarn\s+run\b",
    r"\bmake\b", r"\bmvn\b", r"\bgradle\b",
    r"\bping\b", r"\bcurl\s+-I\b", r"\bcurl\s+--head\b",
    r"\bps\b", r"\btop\b", r"\bhtop\b",
    r"\buname\b", r"\bhostname\b",
    r"\benv\b", r"\bprintenv\b",
    r"\bhistory\b",
    r"\bg[ai]t\s+status\b", r"\bg[ai]t\s+log\b", r"\bg[ai]t\s+diff\b",
    r"\bg[ai]t\s+branch\b",
    r"\bcd\b", r"\bpushd\b", r"\bpopd\b",
    r"\becho\b(?!\s+>)",  # echo 不带重定向
    r"\btype\b", r"\bcommand\b",
    r"\bhelp\b", r"\bman\b", r"\binfo\b",
]


def classify_risk(message: str) -> str:
    """
    根据消息内容判断风险等级。
    
    Returns:
        "high": 高风险，需要用户确认
        "medium": 中风险，建议用户确认（默认由 AstrBot 判断）
        "low": 低风险，自动放行
    """
    if not message:
        return "low"
    
    # 检查高风险
    for pattern in HIGH_RISK_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return "high"
    
    # 检查中风险
    for pattern in MEDIUM_RISK_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return "medium"
    
    # 检查是否仅包含低风险操作
    # 如果全是低风险操作，返回 low
    # 否则返回 medium（无法确定）
    lines = message.strip().split("\n")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 检查这一行是否匹配任何已知模式
        has_known = False
        for pattern in HIGH_RISK_PATTERNS + MEDIUM_RISK_PATTERNS + LOW_RISK_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                has_known = True
                break
        if not has_known:
            # 未知操作，保守返回 medium
            return "medium"
    
    return "low"


def is_high_risk(message: str) -> bool:
    """判断是否高风险操作"""
    return classify_risk(message) == "high"


def is_low_risk(message: str) -> bool:
    """判断是否低风险操作"""
    return classify_risk(message) == "low"


def get_risk_summary(message: str) -> str:
    """获取风险摘要的描述文本"""
    risk = classify_risk(message)
    if risk == "high":
        matched = []
        for p in HIGH_RISK_PATTERNS:
            m = re.search(p, message, re.IGNORECASE)
            if m:
                matched.append(m.group())
        return f"🔴 高风险操作: {', '.join(matched[:3])}" if matched else "🔴 高风险操作"
    elif risk == "medium":
        matched = []
        for p in MEDIUM_RISK_PATTERNS:
            m = re.search(p, message, re.IGNORECASE)
            if m:
                matched.append(m.group())
        return f"🟡 中风险操作: {', '.join(matched[:3])}" if matched else "🟡 中风险操作"
    return "🟢 低风险操作（自动放行）"
