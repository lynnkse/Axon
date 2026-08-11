-- Axon persistent actor model. Apply on ROG's Supabase project first.
create extension if not exists pgcrypto;

create table if not exists events (
  id uuid primary key default gen_random_uuid(),
  sequence bigint generated always as identity unique,
  event_type text not null,
  schema_version integer not null default 1 check (schema_version > 0),
  occurred_at timestamptz not null,
  recorded_at timestamptz not null default now(),
  source_actor_id text,
  source_instance text not null,
  source_kind text not null,
  correlation_id uuid,
  causation_id uuid,
  activation_id uuid,
  idempotency_key text not null unique,
  payload jsonb not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
  provenance jsonb not null default '{}'::jsonb check (jsonb_typeof(provenance) = 'object'),
  assignments jsonb not null default '[]'::jsonb check (jsonb_typeof(assignments) = 'array')
);
create index if not exists events_recorded_idx on events(recorded_at);
create index if not exists events_type_idx on events(event_type);
create index if not exists events_source_actor_idx on events(source_actor_id);
create index if not exists events_correlation_idx on events(correlation_id);
create index if not exists events_activation_idx on events(activation_id);
create index if not exists events_assignments_idx on events using gin(assignments);

create table if not exists actor_state (
  actor_id text primary key,
  actor_type text not null,
  instance text not null,
  state_schema_version integer not null default 1 check (state_schema_version > 0),
  revision integer not null default 0 check (revision >= 0),
  state jsonb not null default '{}'::jsonb check (jsonb_typeof(state) = 'object'),
  directory_projection jsonb not null default '{}'::jsonb check (jsonb_typeof(directory_projection) = 'object'),
  disposition text not null default 'dormant' check (disposition in ('dormant','ready_again','waiting_for_event','waiting_for_human','blocked','completed')),
  last_event_sequence bigint not null default 0,
  last_advanced_at timestamptz,
  dirty boolean not null default false,
  blocked_reason text,
  unblock_at timestamptz,
  cooldown_until timestamptz,
  dependencies jsonb not null default '[]'::jsonb,
  scheduling_class integer not null default 10,
  nice integer not null default 0 check (nice between -20 and 19),
  nice_weight numeric not null default 1 check (nice_weight > 0),
  virtual_deadline numeric not null default 0,
  service_debt numeric not null default 0,
  last_eligible_at timestamptz,
  dormant_since timestamptz,
  quantum_tokens integer not null default 4000 check (quantum_tokens > 0),
  activation_cost_limit numeric not null default 1 check (activation_cost_limit >= 0),
  activation_count bigint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (length(actor_id) > 0)
);
create index if not exists actor_state_sched_idx on actor_state(dirty, scheduling_class, virtual_deadline);

create table if not exists obligations (
  id uuid primary key default gen_random_uuid(),
  owner_actor_id text not null references actor_state(actor_id),
  status text not null default 'open' check (status in ('open','acknowledged','completed','cancelled')),
  kind text not null,
  content jsonb not null default '{}'::jsonb,
  due_at timestamptz,
  qualifying_interaction jsonb not null default '{}'::jsonb,
  escalation_level integer not null default 0 check (escalation_level >= 0),
  escalation_policy jsonb not null default '{}'::jsonb,
  last_presented_at timestamptz,
  presentation_count integer not null default 0 check (presentation_count >= 0),
  min_spacing interval not null default interval '24 hours',
  ack_criteria jsonb not null default '{}'::jsonb,
  completion_criteria jsonb not null default '{}'::jsonb,
  acknowledged_at timestamptz,
  completed_at timestamptz,
  revision integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists obligations_due_idx on obligations(status, due_at);
create index if not exists obligations_owner_idx on obligations(owner_actor_id);

create table if not exists leases (
  actor_id text primary key references actor_state(actor_id) on delete cascade,
  revision integer not null,
  lease_owner text not null,
  lease_expires_at timestamptz not null,
  activation_id uuid not null unique,
  acquired_at timestamptz not null default now(),
  heartbeat_at timestamptz not null default now()
);
create index if not exists leases_expiry_idx on leases(lease_expires_at);

create or replace function reject_event_mutation() returns trigger language plpgsql as $$
begin raise exception 'events are immutable; append a compensating event'; end $$;
drop trigger if exists events_immutable on events;
create trigger events_immutable before update or delete on events for each row execute function reject_event_mutation();

create or replace function validate_event_assignments() returns trigger language plpgsql as $$
declare item jsonb;
begin
  for item in select * from jsonb_array_elements(new.assignments) loop
    if coalesce(item->>'actor_id','')='' or coalesce((item->>'confidence')::numeric,-1) not between 0 and 1 then
      raise exception 'invalid event assignment';
    end if;
  end loop;
  return new;
end $$;
drop trigger if exists events_validate_assignments on events;
create trigger events_validate_assignments before insert on events for each row execute function validate_event_assignments();

create or replace function append_event(p_event jsonb) returns events language plpgsql security definer as $$
declare out_row events;
begin
  select * into out_row from events where idempotency_key=p_event->>'idempotency_key';
  if out_row.id is null then
    begin
      insert into events(event_type,schema_version,occurred_at,source_actor_id,source_instance,source_kind,
        correlation_id,causation_id,activation_id,idempotency_key,payload,provenance,assignments)
      values (p_event->>'event_type',coalesce((p_event->>'schema_version')::int,1),
        coalesce((p_event->>'occurred_at')::timestamptz,now()),p_event->>'source_actor_id',
        p_event->>'source_instance',p_event->>'source_kind',nullif(p_event->>'correlation_id','')::uuid,
        nullif(p_event->>'causation_id','')::uuid,nullif(p_event->>'activation_id','')::uuid,
        p_event->>'idempotency_key',coalesce(p_event->'payload','{}'),coalesce(p_event->'provenance','{}'),
        coalesce(p_event->'assignments','[]')) returning * into out_row;
    exception when unique_violation then
      select * into out_row from events where idempotency_key=p_event->>'idempotency_key';
    end;
  end if;
  update actor_state set dirty=true, updated_at=now()
    where actor_id in (select value->>'actor_id' from jsonb_array_elements(out_row.assignments));
  return out_row;
end $$;

create or replace function present_obligations(p_ids uuid[]) returns setof obligations language sql security definer as $$
  update obligations set last_presented_at=now(),presentation_count=presentation_count+1,
    revision=revision+1,updated_at=now() where id=any(p_ids) and status='open' returning *
$$;

create or replace function acquire_actor_lease(p_actor_id text,p_revision int,p_owner text,p_activation_id uuid,p_ttl_seconds int)
returns leases language plpgsql security definer as $$
declare out_row leases;
begin
  if not exists(select 1 from actor_state where actor_id=p_actor_id and revision=p_revision) then return null; end if;
  insert into leases(actor_id,revision,lease_owner,lease_expires_at,activation_id)
  values(p_actor_id,p_revision,p_owner,now()+make_interval(secs=>p_ttl_seconds),p_activation_id)
  on conflict(actor_id) do update set revision=excluded.revision,lease_owner=excluded.lease_owner,
    lease_expires_at=excluded.lease_expires_at,activation_id=excluded.activation_id,
    acquired_at=now(),heartbeat_at=now()
  where leases.lease_expires_at <= now()
  returning * into out_row;
  return out_row;
end $$;

create or replace function renew_actor_lease(p_actor_id text,p_owner text,p_activation_id uuid,p_ttl_seconds int)
returns boolean language sql security definer as $$
  with u as (update leases set lease_expires_at=now()+make_interval(secs=>p_ttl_seconds),heartbeat_at=now()
    where actor_id=p_actor_id and lease_owner=p_owner and activation_id=p_activation_id and lease_expires_at>now() returning 1)
  select exists(select 1 from u)
$$;

create or replace function release_actor_lease(p_actor_id text,p_owner text,p_activation_id uuid)
returns boolean language sql security definer as $$
  with d as (delete from leases where actor_id=p_actor_id and lease_owner=p_owner and activation_id=p_activation_id returning 1)
  select exists(select 1 from d)
$$;

create or replace function commit_actor_transition(p_actor_id text,p_expected_revision int,p_owner text,
  p_activation_id uuid,p_state jsonb,p_directory jsonb,p_disposition text,p_last_sequence bigint,p_emitted jsonb)
returns actor_state language plpgsql security definer as $$
declare out_row actor_state; item jsonb;
begin
  if not exists(select 1 from leases where actor_id=p_actor_id and revision=p_expected_revision
    and lease_owner=p_owner and activation_id=p_activation_id and lease_expires_at>now()) then return null; end if;
  update actor_state set state=p_state,directory_projection=p_directory,disposition=p_disposition,
    last_event_sequence=p_last_sequence,revision=revision+1,last_advanced_at=now(),updated_at=now(),
    dirty=(p_disposition='ready_again'),activation_count=activation_count+1
  where actor_id=p_actor_id and revision=p_expected_revision returning * into out_row;
  if out_row.actor_id is null then return null; end if;
  for item in select * from jsonb_array_elements(coalesce(p_emitted,'[]')) loop perform append_event(item); end loop;
  delete from leases where actor_id=p_actor_id and lease_owner=p_owner and activation_id=p_activation_id;
  return out_row;
end $$;

revoke update, delete on events from anon, authenticated;
