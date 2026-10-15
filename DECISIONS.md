# DECISIONS.md

### 1. Indexes

A unique index on `employees.emp_code` prevents duplicate employee identities. `(department, emp_code)` supports department filtering and employee ordering, while `(department, joined_on)` supports department-specific headcount queries. The unique `(emp_code, date)` attendance index prevents duplicate daily records. `(date, emp_code)` and `(date, status)` support date-based listing and status filtering. I would avoid a standalone `status` index because it would add write overhead without supporting the main date-filtered query as effectively.

### 2. Punch-in race

Application-level existence checks alone cannot prevent concurrent inserts. The unique `(emp_code, date)` index provides the final guarantee: one insert succeeds, and duplicate-key errors are translated into HTTP 409.

### 3. Ties

Use rank semantics that preserve equal late-minute totals, including ties at the requested cutoff. Sort tied employees by `emp_code` for deterministic output.

### 4. Headcount

Start the department summary from employees and join attendance records. Include employees who joined by the reporting period's end even if they have no attendance logs; exclude later joiners.

### 5. Scaling

At 100 times the data volume, inspect execution plans and query latency, validate indexes against real query patterns, and consider archival or partitioning strategies if measurements justify them.
