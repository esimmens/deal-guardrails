-- Deal Guardrails schema. Postgres is the only writer of state.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TYPE deal_status        AS ENUM ('pending_approval','cleared','approved','rejected','cancelled','superseded','expired');
CREATE TYPE approver_role      AS ENUM ('ae','deal_desk','finance','legal','product','cro');
CREATE TYPE payment_terms_t    AS ENUM ('net_30','net_45','net_60','net_90','annual_prepaid','multi_year_prepaid','other');
CREATE TYPE segment_t          AS ENUM ('smb','mid_market','enterprise','public_sector');
CREATE TYPE extraction_source  AS ENUM ('llm','user_confirmed','user_corrected','default','server_derived');
CREATE TYPE decision_t         AS ENUM ('approve','reject');
CREATE TYPE notification_kind  AS ENUM ('approval_card','requester_dm','thread_update','reminder');
CREATE TYPE notification_state AS ENUM ('pending','sent','failed');

CREATE TABLE policy_versions (
  policy_version text PRIMARY KEY,               -- 'sha256:' || hex of the YAML bytes
  version_label  text NOT NULL,
  yaml_text      text NOT NULL,
  rule_ids       text[] NOT NULL,
  loaded_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
  id              serial PRIMARY KEY,
  name            text NOT NULL,
  name_normalized text NOT NULL UNIQUE,
  segment         segment_t NOT NULL,
  is_strategic    boolean NOT NULL DEFAULT false,  -- set by Deal Desk, never by the agent
  region          text
);

CREATE TABLE employees (
  id            serial PRIMARY KEY,
  full_name     text NOT NULL,
  title         text NOT NULL,
  email         text NOT NULL UNIQUE,
  slack_user_id text UNIQUE,                       -- NULL for employees without a Slack alias
  manager_id    integer REFERENCES employees(id),
  is_active     boolean NOT NULL DEFAULT true
);

CREATE TABLE employee_roles (
  employee_id integer NOT NULL REFERENCES employees(id),
  role        approver_role NOT NULL,
  PRIMARY KEY (employee_id, role)
);

CREATE TABLE price_book (
  sku                    text PRIMARY KEY,
  name                   text NOT NULL,
  unit                   text NOT NULL,            -- '1k_chars','minute','year','one_time'
  list_unit_price_micros bigint NOT NULL,          -- USD * 1e6
  effective_from         date NOT NULL,
  is_assumption          boolean NOT NULL DEFAULT true
);

CREATE SEQUENCE deal_number_seq START 1001;

CREATE TABLE deals (
  id                    bigserial PRIMARY KEY,
  deal_number           integer NOT NULL UNIQUE DEFAULT nextval('deal_number_seq'),
  deal_ref              text GENERATED ALWAYS AS ('DG-' || deal_number::text) STORED,
  requester_employee_id integer NOT NULL REFERENCES employees(id),
  account_id            integer NOT NULL REFERENCES accounts(id),
  account_name_raw      text NOT NULL,
  list_total_cents      bigint NOT NULL CHECK (list_total_cents > 0),
  net_total_cents       bigint NOT NULL CHECK (net_total_cents >= 0),
  acv_cents             bigint NOT NULL,
  discount_bps          integer NOT NULL CHECK (discount_bps BETWEEN 0 AND 10000),
  term_months           integer NOT NULL CHECK (term_months > 0),
  payment_terms         payment_terms_t NOT NULL,
  segment               segment_t NOT NULL,
  status                deal_status NOT NULL,
  policy_version        text NOT NULL REFERENCES policy_versions(policy_version),
  idempotency_key       text NOT NULL UNIQUE,
  supersedes_deal_id    bigint REFERENCES deals(id),
  conversation_id       text,
  raw_request_text      text NOT NULL,
  gate_role             approver_role,
  gate_employee_ids     integer[] NOT NULL DEFAULT '{}',
  receipt               jsonb NOT NULL DEFAULT '{}'::jsonb,
  submitted_at          timestamptz NOT NULL DEFAULT now(),
  decided_at            timestamptz
);
CREATE INDEX deals_status_submitted_idx ON deals (status, submitted_at);
CREATE INDEX deals_conversation_idx     ON deals (conversation_id);

-- The merged (LLM ∪ human) term values the engine actually evaluated.
CREATE TABLE deal_terms (
  deal_id                     bigint PRIMARY KEY REFERENCES deals(id),
  termination_for_convenience boolean NOT NULL,
  nonstandard_legal_terms     boolean NOT NULL,
  outcome_based_pricing       boolean NOT NULL,
  implementation_arrangement  boolean NOT NULL,
  license_fee_restructure     boolean NOT NULL,
  claims_strategic_account    boolean NOT NULL,
  is_renewal                  boolean NOT NULL,
  is_competitive              boolean NOT NULL,
  notes                       text
);

-- What the model read and what a human confirmed never share a column.
CREATE TABLE request_extractions (
  id              bigserial PRIMARY KEY,
  deal_id         bigint NOT NULL REFERENCES deals(id),
  field           text NOT NULL,
  llm_value       jsonb,
  llm_model       text,
  agent_version   text,
  confirmed_value jsonb,
  confirmed_by    integer REFERENCES employees(id),
  confirmed_at    timestamptz,
  source          extraction_source NOT NULL,
  CHECK (source = 'llm' OR confirmed_value IS NOT NULL)
);
CREATE INDEX request_extractions_deal_idx ON request_extractions (deal_id, field);

-- Pinned at submit: what WAS required, separate from what was decided.
CREATE TABLE approval_requirements (
  deal_id  bigint NOT NULL REFERENCES deals(id),
  role     approver_role NOT NULL,
  is_gate  boolean NOT NULL DEFAULT false,
  rule_ids text[] NOT NULL DEFAULT '{}',
  added_by text NOT NULL CHECK (added_by IN ('policy','llm','unresolved')),
  PRIMARY KEY (deal_id, role)
);
CREATE UNIQUE INDEX one_gate_per_deal ON approval_requirements (deal_id) WHERE is_gate;

CREATE TABLE approvals (
  id                   bigserial PRIMARY KEY,
  deal_id              bigint NOT NULL REFERENCES deals(id),
  role                 approver_role NOT NULL,
  approver_employee_id integer NOT NULL REFERENCES employees(id),
  decision             decision_t NOT NULL,
  slack_user_id        text NOT NULL,
  slack_message_ts     text,
  decided_at           timestamptz NOT NULL DEFAULT now(),
  UNIQUE (deal_id, role)
);

-- A Slack user id authenticates. This trigger authorizes, and refuses self-approval,
-- independently of whatever the service handler already checked.
CREATE FUNCTION approvals_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE req integer;
BEGIN
  SELECT requester_employee_id INTO req FROM deals WHERE id = NEW.deal_id;
  IF req IS NULL THEN
    RAISE EXCEPTION 'unknown_deal: %', NEW.deal_id USING ERRCODE = 'check_violation';
  END IF;
  IF req = NEW.approver_employee_id THEN
    RAISE EXCEPTION 'self_approval: employee % is the requester of deal %', req, NEW.deal_id
      USING ERRCODE = 'check_violation';
  END IF;
  -- Authorized if the approver holds the role, or was explicitly designated a gate holder for this
  -- deal by the server (the skip-level case, which is itself audited).
  IF NOT EXISTS (SELECT 1 FROM employee_roles WHERE employee_id = NEW.approver_employee_id AND role = NEW.role)
     AND NOT EXISTS (SELECT 1 FROM deals WHERE id = NEW.deal_id AND NEW.approver_employee_id = ANY(gate_employee_ids)) THEN
    RAISE EXCEPTION 'role_not_held: employee % does not hold % and is not a designated gate holder', NEW.approver_employee_id, NEW.role
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER approvals_guard BEFORE INSERT ON approvals FOR EACH ROW EXECUTE FUNCTION approvals_guard();

-- Outbox. Silent notification loss is the worst failure in an approval system.
CREATE TABLE notifications (
  id              bigserial PRIMARY KEY,
  deal_id         bigint NOT NULL REFERENCES deals(id),
  kind            notification_kind NOT NULL,
  target          jsonb NOT NULL,
  payload         jsonb NOT NULL,
  status          notification_state NOT NULL DEFAULT 'pending',
  attempts        integer NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error      text,
  sent_at         timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX notifications_due_idx ON notifications (status, next_attempt_at) WHERE status = 'pending';

CREATE TABLE conversations (
  conversation_id         text PRIMARY KEY,
  deal_id                 bigint REFERENCES deals(id),
  slack_channel_id        text,
  slack_thread_ts         text,
  requester_slack_user_id text,
  agent_version           text,
  created_at              timestamptz NOT NULL DEFAULT now()
);
