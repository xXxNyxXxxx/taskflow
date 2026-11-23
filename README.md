# TaskFlow

A multi-tenant Kanban and project management platform built with Flask. TaskFlow combines white-label organization governance, 2-tier task hierarchies, cascading deadline controls, revocable `.ics` calendar feeds, and formula-escaped reporting exports inside a modular application factory architecture.

---

## Key Capabilities

* **Multi-Tenant Governance & White-Labeling:** Organization provisioning via validated invite codes, role-based access control (RBAC), Super Admin branding controls (`OrganizationSettings`), and dynamic zero-flash theme rendering (`Calm Alabaster` & `Warm Charcoal`).
* **2-Tier Task & Kanban Workflow:** Interactive AJAX Kanban boards (`X-CSRFToken` protected), parent/subtask hierarchies, deadline extension requests with cascading date caps, and a personal *My Focus* drawer.
* **Operational Governance & Recognition:** Emergency lead handovers, soft member removal, immutable `AuditLog` tracking, and idempotent `PointLedger` tier calculations.
* **Calendar Projections & Revocable Feeds:** JSON/UI calendar views paired with user-scoped, revocable `.ics` subscription tokens.
* **Analytics & Hardened Exports:** Interactive Chart.js dashboards alongside CSV/Excel (formula-injection escaped) and PDF (HTML-escaped) export pipelines.

---

## Architecture Overview

TaskFlow separates HTTP routing, domain logic, and persistence into distinct layers:

```text
taskflow/
├── run.py                  # WSGI entry point & custom Flask CLI commands (e.g., flask seed-dev)
├── config.py               # Environment configs (Development, Testing, Production)
├── requirements.txt
└── app/
    ├── __init__.py         # create_app() factory, Blueprint registration, security headers
    ├── extensions.py       # SQLAlchemy, Migrate, LoginManager, CSRFProtect, Limiter
    ├── models/             # Modular SQLAlchemy models (tenant, user, project, task, governance)
    ├── services/           # Fat service layer (business rules, date caps, RBAC, audit logging)
    ├── blueprints/         # Thin HTTP controllers (auth, admin, projects, board, calendar, analytics)
    ├── static/             # CSS design tokens & vanilla JS AJAX/Chart handlers
    └── templates/          # Jinja2 templates with zero-flash theme initialization