# Employee Attendance & Analytics API

A REST API built with **FastAPI** and **MongoDB** to manage employee attendance, punch-in/punch-out operations, attendance regularization with audit history, and analytics.

## Tech Stack

- **Language:** Python 3.11+
- **API Framework:** FastAPI
- **Database:** MongoDB 6.0+
- **Database Driver:** PyMongo

## Features

- Employee management
- Attendance punch-in and punch-out
- Attendance status regularization with audit history
- Attendance listing with filtering and pagination
- Monthly employee attendance analytics
- Department-wise attendance summaries
- Late-arrival leaderboard
- Department attendance trends
- Query execution-plan inspection through admin explain endpoints
- Database indexes for efficient queries

## Prerequisites

- Python 3.11 or later
- MongoDB 6.0 or later
- Git

MongoDB Atlas or a local MongoDB instance can be used.

## Setup and Installation

Clone the repository:

```bash
git clone https://github.com/nashrah14/hrone-attendance.git
cd hrone-attendance
```

Create and activate a virtual environment on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

On macOS/Linux, activate it with:

```bash
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Environment Configuration

Configure the following environment variables before starting the application:

| Variable | Description |
|---|---|
| `MONGO_URI` | MongoDB connection URI |
| `MONGO_DB` | MongoDB database name |

Set these variables in your local environment or a local `.env` file if the application is configured to load one. Never commit credentials or secrets to GitHub.

## Run the Application

Start the API from the repository root:

```bash
uvicorn app.main:app --port 8000
```

The API will be available at:

- **Health check:** http://127.0.0.1:8000/health
- **Swagger UI:** http://127.0.0.1:8000/docs
- **OpenAPI schema:** http://127.0.0.1:8000/openapi.json

## API Overview

| Area | Purpose |
|---|---|
| Employees | Create and retrieve employee information |
| Attendance | Punch-in, punch-out, list, and regularize attendance |
| Employee analytics | Monthly attendance metrics |
| Department analytics | Department summaries and attendance trends |
| Late-arrival analytics | Rank employees by late minutes |
| Admin explain | Inspect database query execution plans |

Use the Swagger UI to explore the available routes, request parameters, and response schemas.

## Business Rules

The API applies the assignment's attendance rules, including grace-period handling for late arrivals, overtime thresholds, work-hour calculations, half-day classification, working-day calculations, and audit history for manual corrections.

All API timestamps use epoch milliseconds, and attendance dates follow the specified India Standard Time (IST) rules.

## Project Structure

```text
hrone-attendance/
├── app/
│   └── main.py
├── .gitignore
├── requirements.txt
├── README.md
├── REVIEW.md
└── DECISIONS.md
```

Developed as part of the HROne Software Engineer Trainee assignment.
