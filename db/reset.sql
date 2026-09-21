-- Admin-only. Wipes working data, keeps employees, accounts, price book and policy versions.
-- TRUNCATE is not stopped by the row-level append-only trigger; that is why only dg_admin
-- runs this and dg_service cannot.
TRUNCATE audit_events, notifications, approvals, approval_requirements, request_extractions,
  deal_terms, conversations, deals RESTART IDENTITY CASCADE;
ALTER SEQUENCE deal_number_seq RESTART WITH 1001;
