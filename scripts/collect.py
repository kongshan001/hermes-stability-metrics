#!/usr/bin/env python3
"""
Hermes Agent 版本稳定性指标采集脚本
每日 06:00 自动运行，采集各项量化指标
"""
import json
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = "NousResearch/Hermes-Agent"
LOCAL_REPO = "/root/.hermes/hermes-agent"
METRICS_DIR = Path("/root/hermes-stability-metrics/metrics")
REPORTS_DIR = Path("/root/hermes-stability-metrics/reports")
METRICS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ── GitHub API helpers ───────────────────────────────────────────────────────

def gh_api(path: str) -> dict:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[gh_api] {url} → {e}")
        return {}

def gh_releases() -> list:
    return gh_api(f"/repos/{REPO}/releases")

def gh_tags() -> list:
    return gh_api(f"/repos/{REPO}/tags")

def gh_issues(state="open", per_page=100) -> list:
    return gh_api(f"/repos/{REPO}/issues?state={state}&per_page={per_page}")

# ── Git helpers ────────────────────────────────────────────────────────────────

def git(args: str, cwd=LOCAL_REPO) -> str:
    result = subprocess.run(
        args, shell=True, cwd=cwd,
        capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()

def git_tags() -> list:
    out = git("git tag --list | sort -V", cwd=LOCAL_REPO)
    return [t.strip() for t in out.splitlines() if t.strip()]

def local_head() -> str:
    return git("git describe --tags --always HEAD", cwd=LOCAL_REPO)

def latest_remote_tag() -> str:
    releases = gh_releases()
    if releases:
        return releases[0].get("tag_name", "unknown")
    return "unknown"

def commits_behind(remote_tag: str) -> tuple[int, str]:
    """Returns (count, local_describe).
    
    Uses git rev-list --ancestry-path when remote_tag is a descendant of HEAD,
    and git rev-list when HEAD is behind remote_tag (A is ancestor of B).
    Handles divergent branches correctly.
    """
    local = local_head()
    if not remote_tag or remote_tag == "unknown":
        return -1, local
    
    # Check if HEAD is ancestor of remote_tag
    is_ancestor = git(f"git merge-base --is-ancestor HEAD {remote_tag} 2>/dev/null && echo yes || echo no", cwd=LOCAL_REPO)
    
    if "yes" in is_ancestor:
        # HEAD is ancestor of remote_tag: count commits in remote but not in HEAD
        behind = git(f"git rev-list --count HEAD..{remote_tag} 2>/dev/null", cwd=LOCAL_REPO)
    else:
        # Divergent or remote_tag is ancestor: use ancestry-path
        # This counts commits on the path from remote_tag to HEAD (HEAD's newer commits)
        ahead = git(f"git rev-list --count {remote_tag}..HEAD 2>/dev/null", cwd=LOCAL_REPO)
        return int(ahead) if ahead.isdigit() else 0, local
    
    try:
        return int(behind), local
    except:
        return -1, local

def commits_last_7d() -> int:
    # Fetch latest remote refs to get accurate commit counts against remote
    git("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO)
    # Count commits on origin/main in last 7 days (best proxy for project activity)
    out = git("git log --oneline origin/main --since='7 days ago' 2>/dev/null | wc -l", cwd=LOCAL_REPO)
    try:
        return int(out)
    except:
        return -1

def commits_last_30d() -> int:
    git("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO)
    out = git("git log --oneline origin/main --since='30 days ago' 2>/dev/null | wc -l", cwd=LOCAL_REPO)
    try:
        return int(out)
    except:
        return -1

def bugfixes_since(tag: str) -> int:
    git("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO)
    out = git(f"git log --oneline origin/main --since='30 days ago' 2>/dev/null | grep -iE 'fix|bug|hotfix|patch' | wc -l", cwd=LOCAL_REPO)
    try:
        return int(out)
    except:
        return 0

# ── Issue analysis ────────────────────────────────────────────────────────────

def analyze_issues() -> dict:
    open_issues = gh_issues(state="open")
    closed_issues = gh_issues(state="closed")
    
    # Group by month
    from collections import Counter
    open_by_month = Counter(i["created_at"][:7] for i in open_issues if isinstance(i.get("created_at"), str))
    
    # P0/P1
    def is_pX(issue, threshold=2):
        for label in issue.get("labels", []):
            name = label.get("name", "").lower()
            if any(p in name for p in ("p0", "p1", "critical", "high priority", "high-priority")):
                return True
        return False
    
    pX_open = [i for i in open_issues if is_pX(i)]
    pX_closed = [i for i in closed_issues if is_pX(i)]
    
    # Average resolution time for closed P0/P1 (last 30 days)
    now = datetime.now(timezone.utc)
    pX_resolved = []
    for i in pX_closed:
        created = i.get("created_at", "")
        closed_at = i.get("closed_at", "")
        if created and closed_at and "2026" in created:
            try:
                c = datetime.fromisoformat(created.replace("Z", "+00:00"))
                c_at = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                hours = (c_at - c).total_seconds() / 3600
                pX_resolved.append(hours)
            except:
                pass
    
    avg_resolution_hours = sum(pX_resolved) / len(pX_resolved) if pX_resolved else None
    
    # Recent critical fixes
    recent_fixes = [i for i in closed_issues if is_pX(i)][:10]
    recent_fixes = [
        {"number": i["number"], "title": i["title"][:60], "closed_at": i.get("closed_at","")}
        for i in recent_fixes
    ]
    
    return {
        "open_total": len(open_issues),
        "open_by_month": dict(open_by_month),
        "pX_open_count": len(pX_open),
        "pX_open": [{"number": i["number"], "title": i["title"][:60]} for i in pX_open[:10]],
        "pX_closed_count": len(pX_closed),
        "avg_resolution_hours": round(avg_resolution_hours, 1) if avg_resolution_hours else None,
        "recent_fixes": recent_fixes
    }

# ── Release analysis ─────────────────────────────────────────────────────────

def analyze_release() -> dict:
    releases = gh_releases()
    if not releases:
        return {"latest_tag": "unknown", "latest_date": None, "days_since": -1}
    
    latest = releases[0]
    tag = latest.get("tag_name", "unknown")
    date_str = latest.get("published_at", "")
    
    days_since = -1
    if date_str:
        try:
            pub = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            days_since = (datetime.now(timezone.utc) - pub).days
        except:
            pass
    
    return {
        "latest_tag": tag,
        "latest_date": date_str[:10] if date_str else None,
        "days_since": days_since,
        "release_count": len(releases),
        "releases": [
            {"tag": r.get("tag_name"), "date": r.get("published_at", "")[:10]}
            for r in releases[:12]
        ]
    }

# ── Scoring ──────────────────────────────────────────────────────────────────

def score_A_freshness(commits_behind: int) -> int:
    if commits_behind <= 0: return 20
    if commits_behind <= 200: return 18
    if commits_behind <= 500: return 15
    if commits_behind <= 1000: return 12
    if commits_behind <= 2000: return 8
    if commits_behind <= 5000: return 5
    return 2

def score_B_backlog(open_count: int, monthly_growth: int) -> int:
    if open_count <= 50: base = 20
    elif open_count <= 100: base = 17
    elif open_count <= 200: base = 14
    elif open_count <= 500: base = 10
    else: base = 6
    if monthly_growth > 20: base -= 3
    return max(base, 1)

def score_C_fix_efficiency(avg_hours: float | None) -> int:
    if avg_hours is None: return 20
    if avg_hours < 6: return 20
    if avg_hours < 24: return 18
    if avg_hours < 72: return 15
    if avg_hours < 168: return 12
    if avg_hours < 336: return 8
    if avg_hours < 720: return 5
    return 2

def score_D_activity(commits_7d: int) -> int:
    daily = commits_7d / 7
    if daily > 150: return max(13 - 2, 1)
    if daily > 100: return 15
    if daily > 50: return 13
    if daily > 20: return 10
    if daily > 10: return 7
    if daily > 0: return 4
    return 1

def score_E_release(days_since: int) -> int:
    if days_since < 0: return 1
    if days_since <= 3: return 15
    if days_since <= 7: return 14
    if days_since <= 14: return 12
    if days_since <= 30: return 9
    if days_since <= 60: return 5
    return 2

def score_F_security(unfixed_critical: int, avg_fix_hours: float | None) -> int:
    if unfixed_critical > 0: return 1
    if avg_fix_hours is None: return 10
    if avg_fix_hours < 24: return 9
    if avg_fix_hours < 72: return 7
    if avg_fix_hours < 168: return 5
    return 3

def overall_grade(total: float) -> tuple[str, str]:
    if total >= 85: return "🟢 优秀", "版本稳定，可直接使用"
    if total >= 70: return "🔵 良好", "基本稳定，关注后续报告"
    if total >= 55: return "🟡 一般", "建议等待小版本或确认关键 issue 已修复"
    if total >= 40: return "🟠 警示", "谨慎升级，需 review 高优先级 issue"
    return "🔴 不推荐", "存在严重问题，暂缓升级"

# ── Main collection ──────────────────────────────────────────────────────────

def collect() -> dict:
    print("[collect] Starting metrics collection...")
    
    # Fetch fresh data
    print("[collect] Fetching releases...")
    release_info = analyze_release()
    print(f"[collect] Latest release: {release_info['latest_tag']} ({release_info['days_since']} days ago)")
    
    print("[collect] Fetching issues...")
    issue_info = analyze_issues()
    print(f"[collect] Open issues: {issue_info['open_total']}, P0/P1: {issue_info['pX_open_count']}")
    
    print("[collect] Analyzing local git state...")
    local_tag = local_head()
    commits_behind_count, _ = commits_behind(release_info["latest_tag"])
    commits_7d = commits_last_7d()
    commits_30d = commits_last_30d()
    bugfixes = bugfixes_since(release_info["latest_tag"])
    print(f"[collect] Local HEAD: {local_tag}, behind: {commits_behind_count}, 7d commits: {commits_7d}")
    
    # Open issues monthly growth
    months = sorted(issue_info["open_by_month"].keys())
    monthly_growth = 0
    if len(months) >= 2:
        growth = issue_info["open_by_month"][months[-1]] - issue_info["open_by_month"][months[-2]]
        monthly_growth = growth
    
    # Score each dimension
    sA = score_A_freshness(commits_behind_count)
    sB = score_B_backlog(issue_info["open_total"], monthly_growth)
    sC = score_C_fix_efficiency(issue_info.get("avg_resolution_hours"))
    sD = score_D_activity(commits_7d)
    sE = score_E_release(release_info["days_since"])
    # F: unfixed P0/P1 count
    unfixed_pX = issue_info["pX_open_count"]
    sF = score_F_security(unfixed_pX, issue_info.get("avg_resolution_hours"))
    
    total = sA + sB + sC + sD + sE + sF
    grade, recommendation = overall_grade(total)
    
    now = datetime.now(timezone.utc)
    record = {
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now.isoformat(),
        "local_tag": local_tag,
        "remote_tag": release_info["latest_tag"],
        "commits_behind": commits_behind_count,
        "open_issues": issue_info["open_total"],
        "pX_open": issue_info["pX_open_count"],
        "commits_7d": commits_7d,
        "commits_30d": commits_30d,
        "days_since_release": release_info["days_since"],
        "bugfixes_since_release": bugfixes,
        "monthly_issue_growth": monthly_growth,
        "scores": {
            "A_freshness": sA,
            "B_backlog": sB,
            "C_fix_efficiency": sC,
            "D_activity": sD,
            "E_release_timing": sE,
            "F_security": sF,
            "total": total
        },
        "issue_details": {
            "open_by_month": issue_info["open_by_month"],
            "pX_open_list": issue_info["pX_open"],
            "avg_resolution_hours": issue_info["avg_resolution_hours"],
            "recent_fixes": issue_info["recent_fixes"]
        },
        "release_history": release_info["releases"],
        "grade": grade,
        "recommendation": recommendation
    }
    
    return record

def save_and_report(record: dict):
    date = record["date"]
    
    # Save daily JSON
    json_path = METRICS_DIR / f"{date}.json"
    with open(json_path, "w") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    print(f"[save] metrics/{date}.json")
    
    # Update summary.json
    summary_path = METRICS_DIR / "summary.json"
    summary = {}
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)
    summary[date] = record["scores"]
    summary["latest"] = {
        "date": date,
        "grade": record["grade"],
        "total": record["scores"]["total"],
        "recommendation": record["recommendation"]
    }
    with open(summary_path, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"[save] metrics/summary.json")
    
    # Generate report
    r = record
    scores = r["scores"]
    report = f"""# Hermes Agent 版本稳定性日报 — {date}

## 综合评分

**{r['grade']} · {scores['total']}/100 分**

> {r['recommendation']}

## 版本信息

| 维度 | 数值 |
|------|------|
| 本地版本 | `{r['local_tag']}` |
| 远程最新 | `{r['remote_tag']}` |
| 落后 commits | {r['commits_behind']} |
| 距上次发布 | {r['days_since_release']} 天 |

## 六维评分

| 维度 | 权重 | 得分 | 说明 |
|------|------|------|------|
| A. 版本新鲜度 | 20% | **{scores['A_freshness']}** | 落后 {r['commits_behind']} commits |
| B. Issue 积压 | 20% | **{scores['B_backlog']}** | {r['open_issues']} open, 月增 {r['monthly_issue_growth']} |
| C. 修复效率 | 20% | **{scores['C_fix_efficiency']}** | P0/P1 平均存活 {r['issue_details']['avg_resolution_hours']}h |
| D. 代码活跃 | 15% | **{scores['D_activity']}** | 7日 {r['commits_7d']} commits, bugfix {r['bugfixes_since_release']} |
| E. Release 稳定 | 15% | **{scores['E_release_timing']}** | 距发布 {r['days_since_release']} 天 |
| F. 安全补丁 | 10% | **{scores['F_security']}** | P0/P1 未修复 {r['pX_open']} 个 |

## Issue 状态

- Open 总量: **{r['open_issues']}**
- P0/P1 Open: **{r['pX_open']}**
- 月趋势: {r['issue_details']['open_by_month']}

### 当前 P0/P1 Issues
"""
    if r["issue_details"]["pX_open_list"]:
        for iss in r["issue_details"]["pX_open_list"]:
            report += f"- #{iss['number']} {iss['title']}\n"
    else:
        report += "- 无\n"
    
    report += f"""
### 最近修复的 P0/P1 Issues
"""
    if r["issue_details"]["recent_fixes"]:
        for iss in r["issue_details"]["recent_fixes"]:
            report += f"- #{iss['number']} [{iss['closed_at'][:10]}] {iss['title']}\n"
    else:
        report += "- 无\n"
    
    report += f"""
## 趋势对比（历史评分）

"""
    summary_path = METRICS_DIR / "summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)
        dates = sorted([d for d in summary if d != "latest"])
        if dates:
            report += "| 日期 | 总分 | 等级 | A | B | C | D | E | F |\n"
            report += "|------|------|------|---|---|---|---|---|---|\n"
            for d in dates[-14:]:  # last 14 days
                s = summary[d]
                report += f"| {d} | {s['total']} | {'🟢' if s['total']>=85 else '🔵' if s['total']>=70 else '🟡' if s['total']>=55 else '🟠' if s['total']>=40 else '🔴'} | {s['A_freshness']} | {s['B_backlog']} | {s['C_fix_efficiency']} | {s['D_activity']} | {s['E_release_timing']} | {s['F_security']} |\n"
    
    report += f"""
---
*自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
    
    report_path = REPORTS_DIR / f"{date}.md"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"[save] reports/{date}.md")
    
    return report

if __name__ == "__main__":
    record = collect()
    report = save_and_report(record)
    print("\n" + "="*60)
    print(report)

