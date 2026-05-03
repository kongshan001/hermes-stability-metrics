#!/usr/bin/env python3
"""
Hermes Agent 自动更新脚本
基于评分决策是否升级，生成版本差异报告
"""
import json
import subprocess
import urllib.request
import urllib.error
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = "NousResearch/Hermes-Agent"
LOCAL_REPO = "/root/.hermes/hermes-agent"
METRICS_DIR = Path("/root/hermes-stability-metrics/metrics")
CHANGELOGS_DIR = Path("/root/hermes-stability-metrics/changelogs")
CHANGELOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Git helpers ────────────────────────────────────────────────────────────────

def git(args: str, cwd=LOCAL_REPO, timeout=30) -> str:
    result = subprocess.run(
        args, shell=True, cwd=cwd,
        capture_output=True, text=True, timeout=timeout
    )
    return result.stdout.strip() + (result.stderr.strip() if result.stderr else "")

def git_ok(args: str, cwd=LOCAL_REPO, timeout=30) -> bool:
    r = subprocess.run(args, shell=True, cwd=cwd, capture_output=True, timeout=timeout)
    return r.returncode == 0

# ── GitHub API ────────────────────────────────────────────────────────────────

def gh_api(path: str) -> dict:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[gh_api] {url} → {e}")
        return {}

def gh_compare(old_tag: str, new_tag: str) -> dict:
    return gh_api(f"/repos/{REPO}/compare/{old_tag}...{new_tag}")

def gh_commits_between(old_tag: str, new_tag: str, per_page=100) -> list:
    commits = []
    page = 1
    while len(commits) < per_page * 3:  # cap at 300
        data = gh_api(f"/repos/{REPO}/commits?page={page}&per_page={per_page}&since=2026-03-01")
        if not data:
            break
        for c in data:
            if c["commit"]["message"].startswith("Merge pull request"):
                continue  # skip merges
            commits.append({
                "sha": c["sha"][:7],
                "title": c["commit"]["message"].split("\n")[0][:72],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"][:10],
                "url": c["html_url"]
            })
            if len(commits) >= per_page * 3:
                break
        page += 1
    return commits

def gh_prs_between(old_tag: str, new_tag: str) -> list:
    """Get merged PRs between two tags using compare API."""
    data = gh_compare(old_tag, new_tag)
    if not data or "commits" not in data:
        return []
    
    prs = []
    for commit in data.get("commits", []):
        msg = commit.get("commit", {}).get("message", "")
        if msg.startswith("Merge pull request"):
            # Extract PR number from "Merge pull request #123 from ..."
            parts = msg.split()
            if len(parts) >= 3:
                try:
                    pr_num = parts[2]
                    prs.append({
                        "number": pr_num,
                        "sha": commit["sha"][:7],
                        "title": msg.split("\n")[0][:72] if "\n" in msg else msg[:72],
                        "url": commit.get("html_url", f"https://github.com/{REPO}/pull/{pr_num}")
                    })
                except:
                    pass
    return prs

# ── Version info ─────────────────────────────────────────────────────────────

def current_local_tag() -> str:
    git("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO)
    return git("git describe --tags --always HEAD", cwd=LOCAL_REPO)

def latest_remote_tag() -> str:
    data = gh_api(f"/repos/{REPO}/releases/latest")
    return data.get("tag_name", "unknown") if data else "unknown"

def previous_stable_tag() -> str:
    """Get the tag before latest (i.e., latest-1)."""
    data = gh_api(f"/repos/{REPO}/releases?per_page=5")
    if data and len(data) >= 2:
        return data[1].get("tag_name", "unknown")
    return "unknown"

def commits_between_tags(old_tag: str, new_tag: str) -> int:
    """Count commits between two tags (following ancestry)."""
    if old_tag == new_tag:
        return 0
    # Check if old_tag is ancestor of new_tag
    ok = git(f"git merge-base --is-ancestor {old_tag} {new_tag} 2>/dev/null && echo yes || echo no", cwd=LOCAL_REPO)
    if "yes" in ok:
        out = git(f"git rev-list --count {old_tag}..{new_tag} 2>/dev/null", cwd=LOCAL_REPO)
    else:
        out = git(f"git rev-list --count {new_tag}..{old_tag} 2>/dev/null", cwd=LOCAL_REPO)
    try:
        return int(out)
    except:
        return -1

# ── Pre-update checks ─────────────────────────────────────────────────────────

def pre_update_check() -> dict:
    """Verify we're ready to update."""
    checks = {}
    
    # Disk space
    import shutil
    total, used, free = shutil.disk_usage("/")
    checks["disk_free_gb"] = round(free / (1024**3), 1)
    checks["disk_ok"] = free > 500 * 1024 * 1024  # 500MB min
    
    # Git status
    status = git("git status --porcelain", cwd=LOCAL_REPO)
    checks["git_clean"] = status == ""
    checks["git_status"] = status if status else "clean"
    
    # Current branch
    checks["branch"] = git("git rev-parse --abbrev-ref HEAD", cwd=LOCAL_REPO)
    
    # Last tag
    checks["current_tag"] = current_local_tag()
    checks["remote_tag"] = latest_remote_tag()
    
    # Pending changes
    checks["ready"] = checks["disk_ok"] and checks["git_clean"]
    
    return checks

# ── Apply update ─────────────────────────────────────────────────────────────

def apply_update(target_tag: str, backup=True) -> dict:
    """Checkout target tag and reinstall."""
    result = {
        "success": False,
        "old_tag": current_local_tag(),
        "new_tag": target_tag,
        "steps": []
    }
    
    steps = []
    
    # Step 1: Backup current state
    if backup:
        backup_file = METRICS_DIR / "backups" / f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        backup_file.parent.mkdir(exist_ok=True)
        with open(backup_file, "w") as f:
            json.dump({
                "tag": result["old_tag"],
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "git_status": git("git status --porcelain", cwd=LOCAL_REPO),
                "pip_list": subprocess.run(
                    "pip list --format=json 2>/dev/null | python3 -c 'import json,sys; [print(r[\"name\"])'",
                    shell=True, capture_output=True, text=True
                ).stdout[:500]
            }, f, ensure_ascii=False, indent=2)
        steps.append(f"Backup saved: {backup_file.name}")
    
    # Step 2: Fetch latest
    if not git_ok("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO):
        result["error"] = "git fetch failed"
        return result
    steps.append("Fetched latest refs")
    
    # Step 3: Checkout target tag
    if not git_ok(f"git checkout {target_tag} 2>&1", cwd=LOCAL_REPO):
        result["error"] = f"git checkout {target_tag} failed"
        return result
    steps.append(f"Checked out {target_tag}")
    
    # Step 4: pip install
    pip_r = subprocess.run(
        "pip install -e . --break-system-packages 2>&1",
        shell=True, cwd=LOCAL_REPO, capture_output=True, text=True, timeout=120
    )
    if pip_r.returncode != 0:
        result["error"] = f"pip install failed: {pip_r.stderr[-300:]}"
        steps.append(f"pip FAILED: {pip_r.stderr[-200:]}")
        return result
    steps.append("pip install -e . succeeded")
    
    result["steps"] = steps
    result["success"] = True
    return result

# ── Changelog generation ──────────────────────────────────────────────────────

def generate_changelog(old_tag: str, new_tag: str) -> str:
    """Generate a detailed changelog between two versions."""
    now = datetime.now(timezone.utc)
    
    # Get commit count
    commit_count = abs(commits_between_tags(old_tag, new_tag))
    
    # Get PRs via compare API
    compare = gh_compare(old_tag, new_tag)
    prs = []
    commits = []
    
    if compare and "commits" in compare:
        for c in compare.get("commits", []):
            msg = c["commit"]["message"]
            sha = c["sha"][:7]
            date = c["commit"]["author"]["date"][:10]
            author = c["commit"]["author"]["name"]
            
            if msg.startswith("Merge pull request"):
                # This is a PR merge
                lines = msg.split("\n")
                first_line = lines[0]
                try:
                    # "Merge pull request #123 from author/branch"
                    pr_num = first_line.split()[2]
                    title = first_line.split("#")[1].split()[1] if "#" in first_line else first_line[50:]
                    prs.append({
                        "number": pr_num,
                        "sha": sha,
                        "title": msg.split("\n")[0][:80],
                        "author": author,
                        "date": date,
                        "url": f"https://github.com/{REPO}/pull/{pr_num}"
                    })
                except:
                    commits.append({
                        "sha": sha, "title": msg.split("\n")[0][:72],
                        "author": author, "date": date
                    })
            else:
                commits.append({
                    "sha": sha,
                    "title": msg.split("\n")[0][:72],
                    "author": author,
                    "date": date
                })
    
    # Categorize PRs
    categories = {
        "🐛 Bug Fixes": [],
        "🚀 Features": [],
        "📦 Providers": [],
        "🔧 Infrastructure": [],
        "🧹 Chores/Others": []
    }
    
    for pr in prs:
        title_lower = pr["title"].lower()
        if any(k in title_lower for k in ["fix", "bug", "hotfix", "patch"]):
            categories["🐛 Bug Fixes"].append(pr)
        elif any(k in title_lower for k in ["feat", "add", "new", "introduce"]):
            categories["🚀 Features"].append(pr)
        elif any(k in title_lower for k in ["provider", "lm studio", "azure", "openai", "anthropic", "gmi"]):
            categories["📦 Providers"].append(pr)
        elif any(k in title_lower for k in ["ci", "cd", "test", "lint", "refactor", "chore", "deps", "docs"]):
            categories["🧹 Chores/Others"].append(pr)
        else:
            categories["🔧 Infrastructure"].append(pr)
    
    # Build markdown
    md = f"""# Hermes 版本更新报告

**日期**: {now.strftime('%Y-%m-%d %H:%M')}  
**版本**: `{old_tag}` → `{new_tag}`  
**Commits**: {commit_count} | **PRs**: {len(prs)}

---

## 📋 变更摘要

| 类别 | 数量 |
|------|------|
| 🐛 Bug Fixes | {len(categories["🐛 Bug Fixes"])} |
| 🚀 Features | {len(categories["🚀 Features"])} |
| 📦 Providers | {len(categories["📦 Providers"])} |
| 🔧 Infrastructure | {len(categories["🔧 Infrastructure"])} |
| 🧹 Chores/Others | {len(categories["🧹 Chores/Others"])} |

"""
    
    for cat_name, items in categories.items():
        if items:
            md += f"\n### {cat_name}\n\n"
            for item in items[:20]:  # cap at 20 per category
                md += f"- [{item['number']}] {item['title']}\n"
            if len(items) > 20:
                md += f"- ... 还有 {len(items)-20} 个\n"
            md += "\n"
    
    # Breaking changes detection
    breaking = [p for p in prs if "**BREAKING**" in p["title"] or "breaking" in p["title"].lower()]
    if breaking:
        md += "\n## ⚠️ Breaking Changes\n\n"
        for p in breaking:
            md += f"- [{p['number']}] {p['title']}\n"
    
    md += f"""
---

*由 hermes-stability-monitor 自动生成  
*仓库: github.com/kongshan001/hermes-stability-metrics*
"""
    return md

# ── Decision engine ───────────────────────────────────────────────────────────

def decide_update(metrics_record: dict) -> tuple[str, str, str]:
    """
    Decide update strategy based on metrics.
    Returns: (decision, target_tag, reason)
    Decisions: "upgrade_latest" | "upgrade_stable" | "upgrade_minor" | "skip"
    """
    s = metrics_record["scores"]
    total = s["total"]
    f_score = s["F_security"]
    c_score = s["C_fix_efficiency"]
    e_score = s["E_release_timing"]
    pX_open = metrics_record["pX_open"]
    days_since = metrics_record["days_since_release"]
    commits_behind = metrics_record["commits_behind"]
    
    local_tag = metrics_record["local_tag"]
    remote_tag = metrics_record["remote_tag"]
    
    # Security first: unfixed P0/P1 + low security score
    if pX_open > 0 and f_score <= 3:
        return "skip", local_tag, f"⛔ 安全优先：{pX_open} 个未修复 P0/P1，且安全分 {f_score}，暂缓升级"
    
    # Low total score
    if total < 55:
        return "skip", local_tag, f"⛔ 总分 {total} < 55，版本风险过高，暂缓升级"
    
    # High score: upgrade to latest
    if total >= 85 or (f_score >= 9 and c_score >= 15 and e_score >= 12):
        return "upgrade_latest", remote_tag, f"✅ 升级到最新：总分 {total}，安全分 {f_score}，修复效率 {c_score}"
    
    # Good score + stable release
    if total >= 70:
        if days_since >= 2:
            return "upgrade_latest", remote_tag, f"⚠️ 升级到 latest：总分 {total}，距发布 {days_since} 天"
        else:
            return "upgrade_stable", previous_stable_tag(), f"⚠️ 升级到上一稳定版：latest 仅发布 {days_since} 天，先观望"
    
    # Medium score
    if total >= 55:
        return "upgrade_stable", previous_stable_tag(), f"🐌 升级到上一稳定版：总分 {total}，保守升级"
    
    return "skip", local_tag, f"⛔ 总分 {total}，不符合升级条件"

# ── Main ─────────────────────────────────────────────────────────────────────

def run_decision() -> dict:
    """Run the full decision → update → report pipeline."""
    from scripts.collect import collect, save_and_report
    
    # Step 1: Collect metrics
    print("[auto_update] Collecting metrics...")
    metrics = collect()
    
    # Step 2: Decide
    decision, target_tag, reason = decide_update(metrics)
    print(f"[auto_update] Decision: {decision} → {target_tag} ({reason})")
    
    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "decision": decision,
        "target_tag": target_tag,
        "reason": reason,
        "metrics": metrics,
        "update_result": None,
        "changelog": None
    }
    
    # Step 3: Apply update if decided
    if decision.startswith("upgrade"):
        old_tag = metrics["local_tag"]
        
        # Pre-check
        checks = pre_update_check()
        print(f"[auto_update] Pre-check: {checks}")
        
        if not checks["ready"]:
            result["update_result"] = {"success": False, "error": f"Pre-check failed: {checks}"}
        else:
            # Apply
            update_res = apply_update(target_tag)
            result["update_result"] = update_res
            print(f"[auto_update] Update result: {update_res}")
            
            if update_res["success"]:
                # Generate changelog
                changelog_md = generate_changelog(old_tag, target_tag)
                
                # Save changelog
                date = datetime.now().strftime("%Y-%m-%d")
                changelog_path = CHANGELOGS_DIR / f"{date}.md"
                with open(changelog_path, "w") as f:
                    f.write(changelog_md)
                result["changelog"] = str(changelog_path)
                result["changelog_md"] = changelog_md
                print(f"[auto_update] Changelog saved: {changelog_path}")
    
    return result

if __name__ == "__main__":
    result = run_decision()
    print("\n" + "="*60)
    print(f"Decision: {result['decision']}")
    print(f"Target: {result['target_tag']}")
    print(f"Reason: {result['reason']}")
    if result["update_result"]:
        print(f"Update success: {result['update_result'].get('success')}")
    print("="*60)
    if result.get("changelog_md"):
        print(result["changelog_md"])
