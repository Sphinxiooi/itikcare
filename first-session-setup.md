# ItikCare — First Claude Code Session: Setup Checklist + Prompt

## Before you open Claude Code

1. Create a project folder on your machine, e.g. `itikcare/`
2. Put these two files (already made for you) inside it:
   - `itikcare-spec.md`
   - `CLAUDE.md`
3. Figma prototype link (already set below): https://www.figma.com/design/TZqCWEen9aoTE3mQZXDWa6/itikcare?node-id=0-1&t=FBmxO4nrTV7NrB4n-0
4. Have a GitHub account logged in on this machine (you already do)
5. Open the `itikcare/` folder in VS Code, then open the Claude Code panel
6. Turn on **plan mode** (Shift+Tab twice, or `/plan`)

---

## The first-session prompt

Paste this into Claude Code once you're in plan mode.

```
I'm starting a capstone project called ItikCare. Please read itikcare-spec.md
and CLAUDE.md in this folder first — they have the full requirements, tech
stack, data model, and constraints.

Here's what I need for this first session:

1. Set up a Django 5 project using the tech stack in the spec (Python 3.13,
   Django 5, MySQL, Tailwind CSS for styling, scikit-learn for the ML side).
   Create the initial project structure with separate apps that make sense
   for the modules described (something like: accounts, farm, forecasting,
   recommendations — but use your judgment on the actual breakdown).

2. Set up a proper Django .gitignore (exclude .env, __pycache__, venv,
   *.pyc, db files, etc.) before anything gets committed.

3. Initialize git in this folder, make the first commit, then create a new
   GitHub repository called "itikcare" and push this initial commit to it.
   Ask me for confirmation before creating the GitHub repo or pushing.

4. For the dashboard/UI: here's a Figma prototype of the design —
   https://www.figma.com/design/TZqCWEen9aoTE3mQZXDWa6/itikcare?node-id=0-1&t=FBmxO4nrTV7NrB4n-0
   Once the project skeleton is set up, use this to inform the layout and
   styling of the main dashboard page — generate it in Django templates +
   Tailwind CSS (not React), matching the structure and spacing from the
   Figma frame as closely as possible.

Give me a plan first before touching any files. Ask me anything that's
unclear in the spec before you start.
```

---

## What to expect after this

- Claude will likely ask a few clarifying questions (app naming, whether you want a virtualenv, MySQL credentials setup) — answer those, don't skip past them.
- It should show you a plan (files/folders it intends to create) before writing anything, since you're in plan mode.
- When it gets to the GitHub step, it may ask you to confirm or to run `gh auth login` once in your terminal if the GitHub CLI isn't authenticated yet — that's a one-time manual step, everything after is automatic.
- Review the plan, approve, and let it build the skeleton. Don't ask for the full app in one go — get the skeleton + dashboard first, then tackle data logging, then the model, then the prescriptive module as separate sessions.
