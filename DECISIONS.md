# DECISIONS.md

### 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes. The compound index on `(department, emp_code)` supports department filtering and sorted employee listing, while `(department, joined_on)` supports department headcount queries. For attendance, the unique `(emp_code, date)` index prevents duplicate daily records and supports employee-specific lookups. The `(date, emp_code)` and `(date, status)` indexes support date-based listing and filtered attendance queries. I would avoid a standalone `status` index because the compound date-and-status index already supports the main filtered query patterns.

### 2. Punch-in race

When identical punch-in requests arrive together, both may initially find no existing record. The unique `(emp_code, date)` index allows only one insert to succeed. The other request encounters a duplicate-key conflict and returns HTTP 409, preventing duplicate attendance records.

### 3. Ties

The leaderboard assigns equal ranks to employees with equal late-minute totals, skipping the next rank as required. Employees with equal totals are ordered by `emp_code` for deterministic results. The limit is applied after ranking.

### 4. Headcount

The department summary starts from employees rather than attendance logs and joins their records. This includes eligible employees who have no attendance logs, while excluding employees who joined after the reporting period.

### 5. Scaling

For 100 times the data, I would examine execution plans and query latency, optimize indexes using real workloads, and consider partitioning or archival strategies if necessary.