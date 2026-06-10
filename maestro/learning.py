"""Learning layer for Maestro.

Extracts post-run statistics from the SQLite database, computes specialist
performance metrics, summarizes grader feedback loops, and compiles the
learnings into MAESTRO_LESSONS.md.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Dict, List, Any


def generate_lessons_learned(db_path: Path, run_id: str, output_path: Path = Path("MAESTRO_LESSONS.md")) -> None:
    """Read sqlite execution database, calculate stats, and write MAESTRO_LESSONS.md."""
    if not db_path.exists():
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # 1. Fetch Run details
        cursor.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        run_row = cursor.fetchone()
        if not run_row:
            return
        run = dict(run_row)

        # 2. Fetch Tasks details
        cursor.execute("SELECT * FROM tasks WHERE run_id = ?", (run_id,))
        tasks = [dict(row) for row in cursor.fetchall()]

        # 3. Fetch Cost details
        cursor.execute("SELECT * FROM cost_log WHERE run_id = ?", (run_id,))
        costs = [dict(row) for row in cursor.fetchall()]

        # 4. Fetch Feedback history
        cursor.execute("SELECT * FROM feedback_history WHERE run_id = ? ORDER BY iteration", (run_id,))
        feedbacks = [dict(row) for row in cursor.fetchall()]

    except Exception as e:
        print(f"Error reading DB during learning analysis: {e}")
        return
    finally:
        conn.close()

    # Calculate specialist metrics
    # specialist_stats = { name: { total_tasks, completed_tasks, total_retries, avg_duration, total_cost } }
    spec_stats: Dict[str, Dict[str, Any]] = {}
    
    for task in tasks:
        spec = task["specialist"]
        if spec not in spec_stats:
            spec_stats[spec] = {
                "total_tasks": 0,
                "completed_tasks": 0,
                "total_retries": 0,
                "durations": [],
                "total_cost": 0.0
            }
        
        stats = spec_stats[spec]
        stats["total_tasks"] += 1
        if task["status"] == "done":
            stats["completed_tasks"] += 1
        stats["total_retries"] += task["retry_count"]
        if task["duration_sec"] is not None:
            stats["durations"].append(task["duration_sec"])
        if task["estimated_cost"] is not None:
            stats["total_cost"] += task["estimated_cost"]

    # Format Markdown
    md_lines = []
    md_lines.append("# 🧠 Maestro Run Lessons Learned / Bài học kinh nghiệm")
    md_lines.append("")
    md_lines.append("This file contains automatically compiled post-run statistics, specialist success rates, and grader recommendations.")
    md_lines.append("*Tài liệu này chứa thống kê hiệu năng, tỷ lệ thành công của chuyên gia và các khuyến nghị sau lượt chạy.*")
    md_lines.append("")
    
    md_lines.append("## 📊 Run Summary / Tóm tắt lượt chạy")
    md_lines.append(f"- **Run ID**: `{run['run_id']}`")
    md_lines.append(f"- **Project**: `{run['project_name']}`")
    md_lines.append(f"- **Status**: `{run['status'].upper()}`")
    budget_limit = f"${run['max_budget_usd']:.2f}" if run.get("max_budget_usd") else "Unlimited"
    md_lines.append(f"- **Total Cost**: `${run['total_spent_usd']:.6f} / {budget_limit}`")
    md_lines.append(f"- **Created At**: `{run['created_at']}`")
    md_lines.append(f"- **Updated At**: `{run['updated_at']}`")
    md_lines.append("")

    md_lines.append("## 🏆 Specialist Performance / Hiệu năng của Chuyên gia")
    md_lines.append("| Specialist | Total Tasks | Successful Tasks | Total Retries | Success Rate | Avg Duration | Total Cost (USD) |")
    md_lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
    
    for spec, stats in sorted(spec_stats.items()):
        total = stats["total_tasks"]
        succ = stats["completed_tasks"]
        rate = f"{(succ / total) * 100:.1f}%" if total > 0 else "0.0%"
        avg_dur = f"{sum(stats['durations']) / len(stats['durations']):.2f}s" if stats["durations"] else "0.00s"
        cost_str = f"${stats['total_cost']:.6f}"
        md_lines.append(f"| {spec} | {total} | {succ} | {stats['total_retries']} | {rate} | {avg_dur} | {cost_str} |")
    md_lines.append("")

    # Rubric failures and feedback
    md_lines.append("## 🔍 Grader Feedback Loops / Vòng lặp phản hồi của Bộ chấm điểm")
    
    tasks_with_feedback = [t for t in tasks if t["retry_count"] > 0 or t["status"] == "failed"]
    if not tasks_with_feedback:
        md_lines.append("No task retries or failures occurred. Outstanding pipeline quality!")
        md_lines.append("*Không có tác vụ nào cần thử lại hoặc thất bại. Chất lượng pipeline hoàn hảo!*")
        md_lines.append("")
    else:
        for t in tasks_with_feedback:
            md_lines.append(f"### Task: `{t['task_id']}` ({t['specialist']})")
            md_lines.append(f"- **Final Status**: `{t['status'].upper()}`")
            md_lines.append(f"- **Retries**: `{t['retry_count']} / {t['max_retries']}`")
            if t["error_message"]:
                md_lines.append(f"- **Error Message**: `{t['error_message']}`")
            
            # Find feedback loop history
            task_feedbacks = [fb for fb in feedbacks if fb["task_id"] == t["task_id"]]
            if task_feedbacks:
                md_lines.append("- **Grading History**:")
                for fb in task_feedbacks:
                    md_lines.append(f"  - *Iteration {fb['iteration']}* (Score: `{fb['grade_score'] or 0.0:.2f}`):")
                    if fb.get("rubric_failures"):
                        try:
                            failures = json.loads(fb["rubric_failures"]) if isinstance(fb["rubric_failures"], str) else fb["rubric_failures"]
                            for fail in failures:
                                md_lines.append(f"    - Rubric Fail: {fail}")
                        except Exception:
                            md_lines.append(f"    - Raw Failures: {fb['rubric_failures']}")
                    if fb["issues_text"]:
                        md_lines.append(f"    - Grader feedback: *\"{fb['issues_text'].strip()}\"*")
            md_lines.append("")

    # Recommendations and advice
    md_lines.append("## 💡 Advice for Future Runs / Khuyên dùng cho lượt chạy sau")
    
    routing_advice = []
    for spec, stats in spec_stats.items():
        total = stats["total_tasks"]
        succ = stats["completed_tasks"]
        rate = succ / total if total > 0 else 0
        if rate < 0.5:
            routing_advice.append(f"- ⚠️ **{spec}** has a low success rate of {rate*100:.1f}%. Consider inspecting its commands/prompts or replacing it with a more capable specialist.")
            routing_advice.append(f"  *Chuyên gia {spec} có tỷ lệ thành công thấp ({rate*100:.1f}%). Cần kiểm tra lại prompt/command hoặc thay thế chuyên gia khác mạnh hơn.*")
        elif stats["total_retries"] > total * 0.5:
            routing_advice.append(f"- ⏳ **{spec}** required frequent retries ({stats['total_retries']} retries for {total} tasks). You may need to specify clearer rubrics, or improve prompt engineering to get output correct on the first attempt.")
            routing_advice.append(f"  *Chuyên gia {spec} cần thử lại nhiều lần. Nên bổ sung tiêu chí chấm điểm rõ ràng hơn hoặc tối ưu prompt ban đầu.*")

    if not routing_advice:
        routing_advice.append("- ✅ All specialists performed successfully and efficiently. Current prompt routing and budget balance are optimal.")
        routing_advice.append("  *Tất cả chuyên gia đều hoạt động xuất sắc. Cấu hình định tuyến và phân bổ ngân sách hiện tại là tối ưu.*")
        
    md_lines.extend(routing_advice)
    md_lines.append("")

    # Write file
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
