-- The state machine as data. service/app/transitions.py holds the same table in code and a
-- test asserts the two are identical. 'cleared' (nothing required) is terminal.
CREATE TABLE deal_transitions (
  from_status deal_status NOT NULL,
  event       text NOT NULL,
  to_status   deal_status NOT NULL,
  PRIMARY KEY (from_status, event)
);
INSERT INTO deal_transitions VALUES
  ('pending_approval', 'approve',   'approved'),
  ('pending_approval', 'reject',    'rejected'),
  ('pending_approval', 'cancel',    'cancelled'),
  ('pending_approval', 'supersede', 'superseded'),
  ('pending_approval', 'expire',    'expired');
