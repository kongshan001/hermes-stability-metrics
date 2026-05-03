#!/usr/bin/env python3
"""
Hermes Stability Morning Check
每日 06:00 执行：采集指标 → 评估评分 → 决策升级 → 执行更新 → 生成报告 → 推送飞书
"""
import json, subprocess, urllib.request, sys
from datetime import datetime, timezone
from pathlib import Path

REPO = "NousResearch/Hermes-Agent"
LOCAL_REPO = "/root/.hermes/hermes-agent"
METRICS_DIR = Path("/root/hermes-stability-metrics/metrics")
CHANGELOGS_DIR = Path("/root/hermes-stability-metrics/changelogs")
REPORTS_DIR = Path("/root/hermes-stability-metrics/reports")
CHANGELOGS_DIR.mkdir(parents=True, exist_ok=True)

# ── Helpers ───────────────────────────────────────────────────────────────────

def gh_api(path: str) -> dict:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except: return {}

def git(cmd: str, cwd=LOCAL_REPO) -> str:
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=30)
    return (r.stdout + r.stderr).strip()

def git_ok(cmd: str, cwd=LOCAL_REPO) -> bool:
    r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, timeout=30)
    return r.returncode == 0

# ── Metrics ─────────────────────────────────────────────────────────────────

def get_local_tag() -> str:
    git("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO)
    return git("git describe --tags --always HEAD", cwd=LOCAL_REPO)

def get_remote_tag() -> str:
    d = gh_api(f"/repos/{REPO}/releases/latest")
    return d.get("tag_name", "unknown") if d else "unknown"

def get_prev_tag() -> str:
    d = gh_api(f"/repos/{REPO}/releases?per_page=5")
    return d[1].get("tag_name", "unknown") if (d and len(d) >= 2) else "unknown"

def commits_behind_count(remote_tag: str) -> int:
    if not remote_tag or remote_tag == "unknown": return -1
    ok = git(f"git merge-base --is-ancestor HEAD {remote_tag} 2>/dev/null && echo yes || echo no", cwd=LOCAL_REPO)
    if "yes" in ok:
        out = git(f"git rev-list --count HEAD..{remote_tag} 2>/dev/null", cwd=LOCAL_REPO)
    else:
        out = git(f"git rev-list --count {remote_tag}..HEAD 2>/dev/null", cwd=LOCAL_REPO)
    try: return int(out)
    except: return -1

def commits_7d() -> int:
    git("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO)
    out = git("git log --oneline origin/main --since='7 days ago' 2>/dev/null | wc -l", cwd=LOCAL_REPO)
    try: return int(out)
    except: return -1

def bugfixes_30d() -> int:
    out = git("git log --oneline origin/main --since='30 days ago' 2>/dev/null | grep -iE 'fix|bug|hotfix|patch' | wc -l", cwd=LOCAL_REPO)
    try: return int(out)
    except: return 0

def analyze_issues() -> dict:
    open_i = gh_api(f"/repos/{REPO}/issues?state=open&per_page=100")
    closed_i = gh_api(f"/repos/{REPO}/issues?state=closed&per_page=100")
    
    def is_pX(issue):
        for l in issue.get("labels", []):
            n = l.get("name", "").lower()
            if any(p in n for p in ("p0", "p1", "critical", "high priority")): return True
        return False
    
    pX_open = [i for i in (open_i or []) if is_pX(i)]
    
    # Resolution time for closed P0/P1
    from datetime import datetime as dt
    now = datetime.now(timezone.utc)
    resolved_hours = []
    for i in (closed_i or []):
        if not is_pX(i): continue
        c, ct = i.get("created_at", ""), i.get("closed_at", "")
        if c and ct:
            try:
                cv = dt.fromisoformat(c.replace("Z","+00:00"))
                ctv = dt.fromisoformat(ct.replace("Z","+00:00"))
                resolved_hours.append((ctv - cv).total_seconds() / 3600)
            except: pass
    
    avg_h = sum(resolved_hours)/len(resolved_hours) if resolved_hours else None
    
    return {
        "open_total": len(open_i or []),
        "pX_open_count": len(pX_open),
        "pX_open": [{"number": i["number"], "title": i["title"][:60]} for i in pX_open[:8]],
        "avg_resolution_hours": round(avg_h, 1) if avg_h else None
    }

def analyze_release() -> dict:
    d = gh_api(f"/repos/{REPO}/releases/latest")
    if not d: return {"tag": "unknown", "days": -1, "date": None}
    tag = d.get("tag_name", "unknown")
    ds = d.get("published_at", "")
    days = -1
    if ds:
        try:
            pub = datetime.fromisoformat(ds.replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - pub).days
        except: pass
    return {"tag": tag, "days": days, "date": ds[:10] if ds else None}

# ── Scoring ──────────────────────────────────────────────────────────────────

SCORE_RULES = {
    "A": lambda b: 20 if b<=0 else 18 if b<=200 else 15 if b<=500 else 12 if b<=1000 else 8 if b<=2000 else 5 if b<=5000 else 2,
    "B": lambda n: 20 if n<=50 else 17 if n<=100 else 14 if n<=200 else 10 if n<=500 else 6,
    "C": lambda h: 20 if h is None else 20 if h<6 else 18 if h<24 else 15 if h<72 else 12 if h<168 else 8 if h<336 else 5,
    "D": lambda c: 15 if c>100 else 13 if c>50 else 10 if c>20 else 7 if c>10 else 4 if c>0 else 1,
    "E": lambda d: 15 if d<=3 else 14 if d<=7 else 12 if d<=14 else 9 if d<=30 else 5 if d<=60 else 2,
    "F": lambda px, h: 1 if px>0 else 10 if h is None else 9 if h<24 else 7 if h<72 else 5 if h<168 else 3,
}

def score_metrics(m: dict) -> dict:
    sA = SCORE_RULES["A"](m["commits_behind"])
    sB = SCORE_RULES["B"](m["open_issues"])
    sC = SCORE_RULES["C"](m["avg_resolution_hours"])
    sD = SCORE_RULES["D"](m["commits_7d"])
    sE = SCORE_RULES["E"](m["days_since_release"])
    sF = SCORE_RULES["F"](m["pX_open_count"], m["avg_resolution_hours"])
    total = sA + sB + sC + sD + sE + sF
    
    if total >= 85: grade, rec = "🟢 优秀", "版本稳定，可直接使用"
    elif total >= 70: grade, rec = "🔵 良好", "基本稳定，建议升级到最新"
    elif total >= 55: grade, rec = "🟡 一般", "建议升级到上一稳定版"
    elif total >= 40: grade, rec = "🟠 警示", "谨慎升级，需 review 高优先级 issue"
    else: grade, rec = "🔴 不推荐", "存在严重问题，暂缓升级"
    
    return {"A_freshness": sA, "B_backlog": sB, "C_fix_efficiency": sC,
            "D_activity": sD, "E_release_timing": sE, "F_security": sF, "total": total, "grade": grade, "recommendation": rec}

# ── Decision ─────────────────────────────────────────────────────────────────

def decide(scores: dict, m: dict) -> tuple[str, str, str]:
    s, px = scores, m["pX_open_count"]
    t, f, c_e = s["total"], s["F_security"], s["C_fix_efficiency"]
    days = m["days_since_release"]
    
    if px > 0 and f <= 3:
        return "skip", m["local_tag"], f"⛔ 安全优先：{px}个P0/P1未修复，安全分{f}，暂缓"
    if t < 55:
        return "skip", m["local_tag"], f"⛔ 总分{t}<55，风险过高"
    if t >= 85 or (f >= 9 and c_e >= 15 and s["E_release_timing"] >= 12):
        return "upgrade_latest", m["remote_tag"], f"✅ 升级最新：总分{t}，安全分{f}，修复效率{c_e}"
    if t >= 70:
        if days >= 2: return "upgrade_latest", m["remote_tag"], f"⚠️ 升级最新：总分{t}，距发布{days}天"
        return "upgrade_stable", m["prev_tag"], f"⚠️ 升级上一稳定版：latest仅发布{days}天，先观望"
    if t >= 55:
        return "upgrade_stable", m["prev_tag"], f"🐌 升级上一稳定版：总分{t}，保守策略"
    return "skip", m["local_tag"], f"⛔ 总分{t}，不符合升级条件"

# ── Update ────────────────────────────────────────────────────────────────────

def do_update(target_tag: str) -> dict:
    git("git fetch --tags origin main 2>/dev/null", cwd=LOCAL_REPO)
    ok1 = git_ok(f"git checkout {target_tag} 2>&1", cwd=LOCAL_REPO)
    if not ok1: return {"success": False, "error": f"checkout {target_tag} failed"}
    r = subprocess.run("pip install -e . --break-system-packages 2>&1",
        shell=True, cwd=LOCAL_REPO, capture_output=True, text=True, timeout=120)
    if r.returncode != 0: return {"success": False, "error": r.stderr[-300:]}
    return {"success": True, "tag": target_tag, "pip": "ok"}

def generate_changelog_md(old_tag: str, new_tag: str) -> str:
    now = datetime.now(timezone.utc)
    # Get commits via compare
    data = gh_api(f"/repos/{REPO}/compare/{old_tag}...{new_tag}")
    commits_list = []
    prs = []
    if data and "commits" in data:
        for c in data.get("commits", []):
            msg = c["commit"]["message"]
            sha = c["sha"][:7]
            if msg.startswith("Merge pull request"):
                parts = msg.split()
                if len(parts) >= 3:
                    try: prs.append({"num": parts[2], "sha": sha, "title": msg.split("\n")[0][:80]})
                    except: pass
            else:
                commits_list.append({"sha": sha, "msg": msg.split("\n")[0][:72]})
    
    bugfixes = [p for p in prs if "fix" in p["title"].lower()]
    features = [p for p in prs if "feat" in p["title"].lower() or "new " in p["title"].lower()]
    providers = [p for p in prs if any(x in p["title"].lower() for x in ["provider", "azure", "lm studio", "gmi", "openai"])]
    
    md = f"""# Hermes 版本更新报告 · {now.strftime('%Y-%m-%d')}

**版本**: `{old_tag}` → `{new_tag}`
**PRs**: {len(prs)} | **Commits**: {len(commits_list)}

## 📋 变更摘要

| 类型 | 数量 |
|------|------|
| 🐛 Bug Fixes | {len(bugfixes)} |
| 🚀 Features | {len(features)} |
| 📦 Provider 新增/更新 | {len(providers)} |
| 🔧 其他 | {len(prs) - len(bugfixes) - len(features) - len(providers)} |

## 🐛 Bug Fixes ({len(bugfixes)})
"""
    for p in bugfixes[:15]: md += f"- #{p['num']} {p['title']}\n"
    md += f"\n## 🚀 Features ({len(features)})\n"
    for p in features[:15]: md += f"- #{p['num']} {p['title']}\n"
    md += f"\n## 📦 Providers ({len(providers)})\n"
    for p in providers[:10]: md += f"- #{p['num']} {p['title']}\n"
    md += f"\n## 📝 Commit Log ({len(commits_list)})\n"
    for c in commits_list[:20]: md += f"- `{c['sha']}` {c['msg']}\n"
    md += f"\n---\n*自动生成 @ {now.isoformat()}*\n"
    return md

# ── Save ─────────────────────────────────────────────────────────────────────

def save_snapshot(m: dict, scores: dict, decision: str, target_tag: str):
    date = datetime.now().strftime("%Y-%m-%d")
    
    record = {
        "date": date, "timestamp": datetime.now(timezone.utc).isoformat(),
        "local_tag": m["local_tag"], "remote_tag": m["remote_tag"],
        "prev_tag": m["prev_tag"], "commits_behind": m["commits_behind"],
        "commits_7d": m["commits_7d"], "open_issues": m["open_issues"],
        "pX_open": m["pX_open_count"], "days_since_release": m["days_since_release"],
        "avg_resolution_hours": m["avg_resolution_hours"],
        "scores": scores, "decision": decision, "target_tag": target_tag
    }
    
    # Save JSON
    p = METRICS_DIR / f"{date}.json"
    with open(p, "w") as f: json.dump(record, f, ensure_ascii=False, indent=2)
    
    # Update summary
    sp = METRICS_DIR / "summary.json"
    summary = json.load(open(sp)) if sp.exists() else {}
    summary[date] = {"total": scores["total"], "grade": scores["grade"],
                     "A_freshness": scores["A_freshness"], "B_backlog": scores["B_backlog"],
                     "C_fix_efficiency": scores["C_fix_efficiency"], "D_activity": scores["D_activity"],
                     "E_release_timing": scores["E_release_timing"], "F_security": scores["F_security"]}
    summary["latest"] = {"date": date, "grade": scores["grade"], "total": scores["total"]}
    with open(sp, "w") as f: json.dump(summary, f, ensure_ascii=False, indent=2)
    
    return record

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("[morning_check] 启动晨检...")
    
    # 1. Collect
    print("[1/5] 采集指标...")
    local_tag = get_local_tag()
    remote_tag = get_remote_tag()
    prev_tag = get_prev_tag()
    behind = commits_behind_count(remote_tag)
    c7d = commits_7d()
    bf30d = bugfixes_30d()
    issues = analyze_issues()
    release = analyze_release()
    
    m = {
        "local_tag": local_tag, "remote_tag": remote_tag, "prev_tag": prev_tag,
        "commits_behind": behind, "commits_7d": c7d, "bugfixes_30d": bf30d,
        "open_issues": issues["open_total"], "pX_open_count": issues["pX_open_count"],
        "pX_open": issues["pX_open"], "avg_resolution_hours": issues["avg_resolution_hours"],
        "days_since_release": release["days"]
    }
    
    # 2. Score
    print("[2/5] 计算评分...")
    scores = score_metrics(m)
    print(f"      评分: {scores['total']}/100 {scores['grade']}")
    
    # 3. Decide
    print("[3/5] 决策...")
    decision, target, reason = decide(scores, m)
    print(f"      决策: {decision} → {target}")
    print(f"      原因: {reason}")
    
    update_ok = None
    changelog_path = None
    changelog_md = None
    
    # 4. Execute update
    if decision.startswith("upgrade"):
        print(f"[4/5] 执行更新: {m['local_tag']} → {target}...")
        # Pre-check disk + git status
        import shutil
        _, _, free = shutil.disk_usage("/")
        git_clean = git("git status --porcelain", cwd=LOCAL_REPO) == ""
        
        if free < 500_000_000:
            print(f"      ⚠️ 磁盘空间不足: {free//1024**2}MB")
        elif not git_clean:
            print(f"      ⚠️ Git 工作区不干净，跳过更新")
        else:
            old_tag = m["local_tag"]
            up = do_update(target)
            if up["success"]:
                print(f"      ✅ 更新成功: {target}")
                changelog_md = generate_changelog_md(old_tag, target)
                date = datetime.now().strftime("%Y-%m-%d")
                changelog_path = CHANGELOGS_DIR / f"{date}.md"
                with open(changelog_path, "w") as f: f.write(changelog_md)
                update_ok = True
            else:
                print(f"      ❌ 更新失败: {up.get('error','unknown')}")
                update_ok = False
    
    # 5. Save + Report
    print("[5/5] 保存记录...")
    record = save_snapshot(m, scores, decision, target)
    
    # ── Build Feishu message ──────────────────────────────────────────
    s = scores
    r = reason
    
    if decision.startswith("upgrade") and update_ok:
        msg = f"""🚀 **Hermes 版本更新 · {datetime.now().strftime('%Y-%m-%d')}**

✅ **决策**: {decision.replace('upgrade_','')} · {m['local_tag']} → `{target}`
📊 **评分变化**: {s['total']}/100 {s['grade']}
🔢 **六维得分** | A:{s['A_freshness']} B:{s['B_backlog']} C:{s['C_fix_efficiency']} D:{s['D_activity']} E:{s['E_release_timing']} F:{s['F_security']}

💡 *{r}*

📋 **完整变更列表**: https://github.com/kongshan001/hermes-stability-metrics/blob/main/changelogs/{datetime.now().strftime('%Y-%m-%d')}.md"""
    else:
        skip_reason = m['local_tag'] if decision == "skip" else "git工作区不干净"
        msg = f"""🤖 **Hermes 版本稳定性日报 · {datetime.now().strftime('%Y-%m-%d')}**

📊 **综合评分**: {s['total']}/100 分 · {s['grade']}
🔢 **六维** | A:{s['A_freshness']} B:{s['B_backlog']} C:{s['C_fix_efficiency']} D:{s['D_activity']} E:{s['E_release_timing']} F:{s['F_security']}

📦 **版本状态**
• 本地: `{m['local_tag']}`
• 远程: `{m['remote_tag']}`
• 落后: {m['commits_behind']} commits | {m['days_since_release']}天前发布

⚠️ **P0/P1 未修复**: {m['pX_open_count']} 个
💡 **建议**: {r}

📈 **完整报告**: https://github.com/kongshan001/hermes-stability-metrics/blob/main/reports/{datetime.now().strftime('%Y-%m-%d')}.md"""
    
    print("\n" + "="*60)
    print(msg)
    print("="*60)
    
    # Return for cron delivery
    return {
        "scores": scores, "decision": decision, "target_tag": target,
        "reason": reason, "record": record, "changelog_path": str(changelog_path) if changelog_path else None,
        "feishu_msg": msg, "update_ok": update_ok
    }

if __name__ == "__main__":
    result = main()
    # Output for cron capture
    with open("/tmp/morning_check_result.json", "w") as f:
        json.dump({k: v for k, v in result.items() if k != "feishu_msg"}, f, ensure_ascii=False, default=str)
    print(f"\nResult saved to /tmp/morning_check_result.json")
