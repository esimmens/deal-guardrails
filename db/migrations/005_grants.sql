GRANT CONNECT ON DATABASE dg TO dg_service, dg_reader;
GRANT USAGE ON SCHEMA public TO dg_service, dg_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO dg_reader;

GRANT SELECT, INSERT, UPDATE ON deals, deal_terms, request_extractions, approval_requirements,
  approvals, notifications, conversations, accounts, policy_versions TO dg_service;
GRANT SELECT ON employees, employee_roles, price_book, deal_transitions TO dg_service;
-- The service can add audit rows and read them. It can never change or remove one.
GRANT SELECT, INSERT ON audit_events TO dg_service;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO dg_service;
