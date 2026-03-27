"""AI service for transcript analysis, task extraction, chat, and agenda generation."""
from __future__ import annotations

import json
import os
from datetime import datetime
from anthropic import AsyncAnthropic

client = AsyncAnthropic(api_key=os.getenv("CLAUDE_API_KEY"))
MODEL = "claude-sonnet-4-20250514"


async def analyze_transcript(transcript: str, members_list: str) -> dict:
    """Analyze a meeting transcript and extract structured data."""
    prompt = f"""You are an AI secretary for a Board of Directors. Analyze the following meeting transcript.

Known board members: {members_list}

Extract the following in JSON format:
{{
  "title": "short meeting title in Russian",
  "date": "YYYY-MM-DD if found, else null",
  "participants": ["list of participant names found"],
  "summary": "structured summary in Russian, organized by topics discussed",
  "tasks": [
    {{
      "title": "task description in Russian",
      "assignee_name": "name of responsible person (must match one of known members, or null if unclear)",
      "deadline": "YYYY-MM-DD if mentioned, else null",
      "priority": "high/medium/low",
      "context_quote": "exact quote from transcript that this task comes from"
    }}
  ],
  "decisions": [
    {{
      "text": "what was decided in Russian",
      "context_quote": "relevant quote"
    }}
  ],
  "open_questions": [
    {{
      "text": "unresolved question in Russian",
      "context_quote": "relevant quote"
    }}
  ],
  "agenda_next": [
    {{
      "topic": "topic for next meeting in Russian",
      "presenter": "who should present (name or null)",
      "estimated_minutes": 15,
      "reason": "why this should be on the agenda"
    }}
  ],
  "task_status_updates": [
    {{
      "task_title_hint": "title or fragment of the task being reported on (in Russian)",
      "assignee_name": "name of person who reported the status",
      "new_status": "done or in_progress",
      "context_quote": "exact quote from transcript where the status was reported"
    }}
  ]
}}

IMPORTANT:
- Write all content in Russian
- Be precise with assignee names — match them to known members
- Extract deadlines when explicitly or implicitly mentioned
- Include context quotes so decisions can be traced back
- For agenda_next, include items where someone promised to report back or present something
- For task_status_updates: capture any moment when a participant says a task/assignment is done,
  completed, finished, or still in progress. Examples: "задача выполнена", "мы это сделали",
  "готово", "ещё не успели", "в процессе". Only include if a specific task is clearly identifiable.

TRANSCRIPT:
{transcript}"""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text
    # Extract JSON from response
    try:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "Failed to parse AI response", "raw": text}


async def chat_with_context(
    user_message: str,
    user_name: str,
    context_chunks: list[str],
    tasks_summary: str,
    user_role: str = "член совета директоров",
    my_tasks_summary: str | None = None,
    task_context: str | None = None,
) -> str:
    """Answer user's question using meeting history and task data as context."""
    context = "\n\n---\n\n".join(context_chunks) if context_chunks else "No meeting records yet."

    my_tasks_block = ""
    if my_tasks_summary:
        my_tasks_block = (
            f"\nTASKS ASSIGNED TO {user_name} (their personal tasks only):\n"
            f"{my_tasks_summary}\n"
        )

    task_context_block = ""
    if task_context:
        task_context_block = (
            f"\nCURRENT TASK (user is viewing this task right now — answer questions about THIS specific task):\n"
            f"{task_context}\n"
        )

    prompt = f"""You are an AI secretary for a Board of Directors. You help board members
by answering questions about meetings, tasks, and decisions.

You are speaking with: {user_name}
Their role: {user_role}
{task_context_block}{my_tasks_block}
MEETING HISTORY (relevant excerpts):
{context}

ALL BOARD TASKS SUMMARY:
{tasks_summary}

Answer the user's question in Russian. Be concise and specific.

CRITICAL RULES:
1. If the user wants to CREATE A NEW TASK or ASSIGN a task to someone — DO NOT do it yourself.
   Instead reply: "Чтобы поставить новую задачу, используй команду /newtask — опиши голосом или текстом, кому и что нужно сделать."
   NEVER try to reassign existing tasks or create tasks through conversation.
2. If "CURRENT TASK" is shown above, the user is asking about THAT specific task — answer based on it directly without asking for clarification.
3. When {user_name} asks about their own tasks, refer to the "TASKS ASSIGNED TO {user_name}" section.
4. If referencing a meeting, mention its date.
5. If referencing a task, mention its status and deadline.
6. If you don't have enough information, say so honestly.

USER QUESTION: {user_message}"""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text


async def parse_stakeholder_task(text: str, members_list: str, previous_parsed: dict | None = None) -> dict:
    """Parse a stakeholder's task description into structured fields."""
    if previous_parsed:
        prompt = f"""You are an AI secretary for a Board of Directors.
A task already exists and the user wants to CORRECT or ADD details to it.
Apply only the changes the user describes — keep everything else from the existing task unchanged.

Known board members: {members_list}
Today: {datetime.now().strftime('%Y-%m-%d')}

EXISTING TASK:
{{
  "title": "{previous_parsed.get('title', '')}",
  "description": "{previous_parsed.get('description', '')}",
  "assignee_name": "{previous_parsed.get('assignee_name', '')}",
  "deadline": "{previous_parsed.get('deadline', '')}",
  "priority": "{previous_parsed.get('priority', 'high')}"
}}

USER CORRECTION: {text}

Return the updated task as JSON with the same fields.
Only change fields explicitly mentioned in the correction. Keep all other fields exactly as in the existing task."""
    else:
        prompt = f"""You are an AI secretary for a Board of Directors.
A shareholder/stakeholder has described a task they want to assign. Extract the structured fields.

Known board members: {members_list}

Extract and return JSON:
{{
  "title": "concise task title in Russian (max 100 chars)",
  "description": "full task description in Russian",
  "assignee_name": "name of the responsible person (must closely match one of the known members, or null)",
  "deadline": "YYYY-MM-DD if a date/timeframe is mentioned, else null",
  "priority": "high/medium/low — default high for stakeholder tasks"
}}

Rules:
- Write title and description in Russian
- Match assignee to the closest known member name
- If deadline is relative (e.g. 'до пятницы', 'через неделю'), calculate from today {datetime.now().strftime('%Y-%m-%d')}
- If unclear, set field to null
- Priority defaults to high for stakeholder assignments

TASK DESCRIPTION:
{text}"""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = response.content[0].text
    try:
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0]
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"title": text[:100], "description": text, "assignee_name": None, "deadline": None, "priority": "high"}


async def generate_agenda(
    meetings_context: str,
    open_tasks: str,
    overdue_tasks: str,
    agenda_items_from_meetings: str,
) -> str:
    """Generate agenda for the next board meeting."""
    prompt = f"""You are an AI secretary for a Board of Directors.
Generate a structured agenda for the next meeting based on:

1. PREVIOUS MEETINGS CONTEXT (recent summaries):
{meetings_context}

2. AGENDA ITEMS PROMISED AT PREVIOUS MEETINGS:
{agenda_items_from_meetings}

3. TASKS WITH APPROACHING DEADLINES (need status report):
{open_tasks}

4. OVERDUE TASKS (need explanation):
{overdue_tasks}

Generate the agenda in Russian with this format:

AGENDA — Board of Directors Meeting, [suggest next date]

For each item:
- [Estimated minutes] Presenter — Topic
  Basis: which meeting/task it comes from
  Task status if relevant

At the end:
- Total estimated duration
- Number of overdue tasks requiring attention

Be specific. Reference actual task IDs and meeting dates.
Prioritize: overdue items first, then promised presentations, then open questions."""

    response = await client.messages.create(
        model=MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )

    return response.content[0].text
