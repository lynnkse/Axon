# Ailin Supabase least privilege design

Status: design only. No schema, grants, policies, credentials, or API settings have been changed.

## Current finding

`ailin_supabase.py` uses `AILIN_SUPABASE_SERVICE_ROLE_KEY` for every GET, POST,
PATCH, UPSERT, and RPC call. The service role bypasses row level security, so the
runtime currently has the effective authority of the backend rather than an
Ailin scoped workspace identity.

## Proposed boundary

1. Create a dedicated `ailin_workspace` schema and expose only that schema to
   Ailin's API client.
2. Give Ailin a dedicated authenticated identity. Store its refresh credential
   outside the sandbox and inject only a short lived access token.
3. Put `owner_id uuid not null` on every workspace table. Enable and force row
   level security. CRUD policies require `owner_id = auth.uid()` and prevent an
   update from changing `owner_id`.
4. Grant the runtime `USAGE` on `ailin_workspace` plus SELECT, INSERT, UPDATE,
   and DELETE only on approved workspace tables and sequences. Do not grant
   privileges on `public`, `auth`, `storage`, extensions, system catalogs, or
   functions outside the allowlist.
5. Remove the service role key from the Ailin process and sandbox. Keep it only
   in an Axon controlled broker when an administrative operation is required.

## Constrained table creation

Do not grant `CREATE` on the schema to the runtime. Route requests through an
Axon owned broker that:

- accepts a table name matching `^[a-z][a-z0-9_]{0,47}$`;
- accepts only a small column type allowlist initially: text, boolean, integer,
  numeric, timestamptz, uuid, and jsonb;
- rejects SQL text, expressions, generated clauses, foreign schemas, functions,
  triggers, and custom defaults;
- enforces limits on tables, columns, rows, and stored bytes;
- creates the table with mandatory `id`, `owner_id`, `created_at`, and
  `updated_at` columns, enables and forces RLS, installs the standard owner
  policies, and applies the same narrow grants in one transaction;
- records requester, normalized specification, result, and migration identifier
  in an append only audit table.

The first version should require human approval for each creation request.
Routine CRUD on already approved tables can run without approval.

## Verification before rollout

- With the Ailin token, prove allowed CRUD succeeds on its own rows.
- Prove reads and writes to another owner fail.
- Prove access to existing `public` tables and administrative RPCs fails.
- Prove arbitrary DDL and direct table creation fail.
- Prove malformed and over quota broker requests fail without partial objects.
- Rotate the current service role credential after the runtime no longer uses it.

## Authorization boundary

Implementation requires explicit authorization to create the schema, change API
schema exposure, add roles or identities, add RLS policies and grants, deploy the
broker, issue credentials, and rotate the current service role key.
