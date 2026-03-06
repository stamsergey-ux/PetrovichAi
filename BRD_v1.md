# AI-Secretary for Board of Directors — Business Requirements Document v1.0

**Date:** 2026-03-05
**Status:** Approved
**Version:** 1.0

---

## 1. Product Summary

Telegram-bot acting as an AI-secretary for the Board of Directors (12 members).
Accepts meeting transcripts, extracts tasks, distributes them among participants,
reminds about deadlines, and answers any questions about meeting history
in a free conversational format.

---

## 2. Users and Roles

| Role | Who | Capabilities |
|------|-----|-------------|
| **Chairman** | @s5069561, @Sergstam | Upload transcripts, review tasks, dashboard, control, manual task creation, full access |
| **Board Member** | 12 people (to be added later) | View all protocols and tasks, manage own tasks, AI chat |

**Identification:** by Telegram username. Names and avatars are taken from Telegram profile.

**Access:** all 12 members see all protocols and all tasks. Additionally, each can filter "my tasks only".

---

## 3. Data Sources

### 3.1. Primary — Plaud.ai (manual upload)
- Chairman sends a ready transcript from Plaud to the bot (text, PDF, or file)
- Transcript contains: date, participants, speaker separation, topics, next steps
- No automatic integration with Plaud in v1 — manual upload via Telegram (Share -> Telegram -> bot)

### 3.2. Backup — iPhone Voice Recorder
- Chairman sends an audio file (m4a/mp3/ogg)
- Bot transcribes via OpenAI Whisper API
- Then processes the same way as Plaud text

---

## 4. Core Workflow

```
Step 1.  Chairman sends transcript/audio to bot
            |
Step 2.  Bot processes:
         - If audio -> Whisper transcription -> text
         - If text/file from Plaud -> takes as is
            |
Step 3.  Claude AI analyzes transcript and extracts:
         - Structured protocol (summary by topics)
         - Tasks (what to do)
         - Assignees (which board member)
         - Deadlines (by when)
         - Decisions (what was approved / rejected)
         - Open questions (what was postponed)
         - Agenda items for next meeting (who presents what)
            |
Step 4.  Bot sends results to Chairman for review:
         "Found 8 tasks, 3 decisions, 2 open questions. Review."
         Chairman can edit, confirm, or reject
            |
Step 5.  After confirmation:
         - Protocol is saved to database
         - Group chat -> summary: "Protocol from March 5 uploaded,
           8 tasks created, 5 assignees"
         - Each board member in DM -> their personal tasks
```

---

## 5. Functional Blocks

### 5.1. Protocol Upload and Processing
- Accept text, files (.txt, .pdf, .docx) and audio (.m4a, .mp3, .ogg)
- Split large audio into chunks (Whisper limit 25 MB)
- AI analysis via Claude API
- Chairman review before publication

### 5.2. Task Management
- Automatic task creation from protocol
- Manual task creation by Chairman
- Each task has: description, assignee, deadline, priority, status, context quote from protocol
- Statuses: New -> In Progress -> Done / Overdue
- Mark completion via inline buttons or text
- Comments on tasks

### 5.3. Viewing and Access
- All board members see all protocols and all tasks
- Filter "my tasks" — personal list
- View protocols by date
- View tasks by status, assignee, deadline

### 5.4. AI Chat (core value)
Free conversational interface. Each board member can ask:

**About tasks:**
- "What are my open tasks?"
- "What is overdue for Alexey?"
- "Who is working on logistics?"

**About protocols:**
- "What was discussed at the last meeting?"
- "What was decided about the budget in February?"
- "What exactly did Mikhail say about marketing?"

**Analytics and control:**
- "What tasks haven't been closed in the last 3 months?"
- "Prepare an agenda — what's left from previous meetings?"
- "Did we ever discuss entering the Kazakhstan market?"

Works via RAG: as protocols accumulate, bot searches semantically in vector database, finds relevant fragments and formulates an answer.

### 5.5. Notifications and Reminders
- 2 days before deadline -> soft reminder to assignee in DM
- On deadline day -> "Today is the deadline for task X"
- After deadline -> "Overdue" + notification to Chairman
- New protocol uploaded -> summary to group chat

### 5.6. Communication Model (Hybrid — Option B)

**Group chat** (bot added to group):
- Protocol summaries
- Weekly digest: X tasks closed, Y overdue
- General announcements

**DM with bot** (each participant):
- Personal tasks and reminders
- AI chat: questions about protocols, clarifications
- Task completion marking

### 5.7. Gantt Chart Export (PDF)
- Generate PDF with Gantt chart of all tasks
- Filter by assignee, period, status
- Color coding: done (green) / in progress (blue) / overdue (red) / not started (gray)
- Sent as file directly in chat
- Can request: "Gantt for Ekaterina" or "Gantt for March"

### 5.8. Auto-generation of Next Meeting Agenda
- Chairman writes "Prepare agenda" or bot suggests 1-2 days before planned meeting date
- AI analyzes: all previous protocols, presentation agreements, tasks with approaching deadlines, open questions, overdue tasks
- Generates structured agenda with:
  - Topic and presenter for each block
  - Estimated duration per block
  - Basis — which protocol/task it came from
  - Status of related tasks
  - Highlights overdue and recurring unresolved questions
- Chairman can edit and approve
- After approval — agenda is sent to group chat and personally to presenters

### 5.9. Intro and Onboarding
- Triggers on: first /start, chairman adds new member, /help button
- Shows bot capabilities in friendly format
- Three quick-action inline buttons: [My Tasks] [Last Protocol] [What can bot do?]
- Chairman sees additional management block (upload, create tasks, agenda, Gantt, dashboard)

---

## 6. Technical Stack

| Component | Technology |
|-----------|-----------|
| Bot | Python + aiogram 3 |
| AI analysis and chat | Claude API (Anthropic) |
| Transcription (backup) | OpenAI Whisper API |
| Database | SQLite (v1) -> PostgreSQL (v2) |
| Vector search (RAG) | Embedded in SQLite (v1) -> Qdrant (v2) |
| PDF generation | matplotlib + reportlab |
| Deploy | Railway |
| Confidentiality | Cloud APIs acceptable for v1 |

---

## 7. Implementation Phases

| Phase | What's included |
|-------|----------------|
| **v0.1** | Upload Plaud transcript -> AI analysis -> tasks with review -> distribution to participants |
| **v0.2** | AI chat: "my tasks", completion marking, questions about protocols |
| **v0.3** | Deadline reminders, weekly digest to group chat |
| **v0.4** | RAG across all meetings — full history search |
| **v0.5** | Backup input: audio from voice recorder -> Whisper -> full pipeline |
| **v0.6** | Gantt chart PDF export |
| **v0.7** | Auto-generation of next meeting agenda |
| **v0.8** | Intro and onboarding flow |
| **v1.0** | Stabilization, adding all 12 participants, production |

---

## 8. Out of Scope for v1
- Self-hosted LLM (to be discussed later)
- Telegram Mini App / web dashboard
- Integration with external systems (Jira, Notion, etc.)
- Automatic integration with Plaud.ai API
- Automatic Plaud.ai import (email parsing or cloud folder)

---

## 9. Participants for v1 (initial)
- @s5069561 (Chairman)
- @Sergstam (Chairman)
- Remaining 10 members to be added after launch
