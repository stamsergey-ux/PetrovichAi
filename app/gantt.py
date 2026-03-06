"""Gantt chart PDF generation for task visualization."""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas as pdf_canvas


def generate_gantt_pdf(tasks: list[dict]) -> io.BytesIO:
    """Generate a Gantt chart PDF from a list of tasks.

    Each task dict: {title, assignee, deadline, created_at, status, id}
    """
    if not tasks:
        return _generate_empty_pdf()

    fig, ax = plt.subplots(1, 1, figsize=(16, max(6, len(tasks) * 0.6)))

    colors = {
        "done": "#4CAF50",
        "in_progress": "#2196F3",
        "new": "#9E9E9E",
        "overdue": "#F44336",
    }

    today = datetime.now()
    labels = []

    for i, task in enumerate(tasks):
        start = task.get("created_at", today - timedelta(days=7))
        end = task.get("deadline", today + timedelta(days=14))
        if isinstance(start, str):
            start = datetime.fromisoformat(start)
        if isinstance(end, str):
            end = datetime.fromisoformat(end)

        status = task.get("status", "new")
        if status == "done":
            color = colors["done"]
        elif end < today and status != "done":
            color = colors["overdue"]
        elif status == "in_progress":
            color = colors["in_progress"]
        else:
            color = colors["new"]

        duration = (end - start).days or 1
        ax.barh(i, duration, left=mdates.date2num(start), height=0.5,
                color=color, alpha=0.8, edgecolor="white", linewidth=0.5)

        assignee = task.get("assignee", "?")
        label = f"#{task.get('id', '?')} {task['title'][:40]}"
        if len(task['title']) > 40:
            label += "..."
        labels.append(f"{label} [{assignee}]")

    ax.set_yticks(range(len(tasks)))
    ax.set_yticklabels(labels, fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))

    # Today line
    ax.axvline(mdates.date2num(today), color="red", linestyle="--", linewidth=1, alpha=0.7)
    ax.text(mdates.date2num(today), len(tasks), " Сегодня", fontsize=7, color="red", va="bottom")

    # Legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor=colors["done"], label="Выполнено"),
        Patch(facecolor=colors["in_progress"], label="В работе"),
        Patch(facecolor=colors["new"], label="Не начато"),
        Patch(facecolor=colors["overdue"], label="Просрочено"),
    ]
    ax.legend(handles=legend_elements, loc="upper right", fontsize=8)

    ax.set_title("Совет Директоров — Диаграмма задач", fontsize=14, fontweight="bold")
    ax.invert_yaxis()
    ax.grid(axis="x", alpha=0.3)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="pdf", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf


def _generate_empty_pdf() -> io.BytesIO:
    buf = io.BytesIO()
    c = pdf_canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica", 16)
    c.drawString(200, 500, "No tasks to display")
    c.save()
    buf.seek(0)
    return buf
