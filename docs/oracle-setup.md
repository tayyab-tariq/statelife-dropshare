# Oracle 19c setup — StateLife DropShare

Requirements: D3, OQ-7, §12. Read this before the first server task.

## 1. The short answer

In Oracle, a **schema is simply a database user**. The objects inside it (tables, indexes,
sequences) are created later by our Alembic migrations, never by hand.

Only two things must already exist on the server:
- the database itself (you have it), and
- a tablespace to store data in (the default `USERS` tablespace is normally fine).

So **yes, you can create the dev and test schemas yourselves**, provided you have a database
account that is allowed to create users (`SYSTEM`, or any account with the `CREATE USER`
privilege). If you don't, give section 4 to your DBA to run; it takes a minute.

## 2. What you need besides the host IP

| # | Item | Why it matters | How to find it |
|---|---|---|---|
| 1 | **Host IP** | — | ✅ You have it. |
| 2 | **Port** | Usually `1521`. | Ask the DBA, or run `lsnrctl status` on the DB server. |
| 3 | **PDB service name** | 19c is normally multitenant. Our schemas must live in a *pluggable* database (PDB), not the root container. Connecting to the wrong service is the most common setup mistake. | Ask the DBA, or run `lsnrctl status`. When connected to the PDB: `SELECT sys_context('USERENV','SERVICE_NAME') FROM dual;` |
| 4 | **Network access** from developer machines | Your Mac (and the Windows test machine, if it runs the server) must reach the port. | macOS Terminal: `nc -vz <host> 1521` → `succeeded`. Windows PowerShell: `Test-NetConnection <host> -Port 1521` → `TcpTestSucceeded : True` |
| 5 | **An account that can create users** (or a DBA to run section 4) | Needed only once, to create the two schemas. | — |
| 6 | **Tablespace** with free space | Where our tables are stored. | `SELECT tablespace_name FROM dba_tablespaces;` (DBA). Default: `USERS`. |
| 7 | **Character set = `AL32UTF8`** | File names can contain any language. If the database isn't Unicode, the data model must use national character types, so I need to know **before** we write models. | `SELECT parameter, value FROM nls_database_parameters WHERE parameter IN ('NLS_CHARACTERSET','NLS_NCHAR_CHARACTERSET');` |
| 8 | **Network encryption policy** | We use `python-oracledb` in *thin* mode (no Oracle client install needed). Thin mode doesn't support Oracle Native Network Encryption. If the server *requires* it, connections fail with `DPY-3001`. Then we either use TLS (TCPS) instead, or switch to *thick* mode, which needs Oracle Instant Client on every server machine. | Ask the DBA whether `SQLNET.ENCRYPTION_SERVER = REQUIRED`, or just try section 5. |
| 9 | **Edition** (Enterprise or Standard 2) | Decides whether Oracle TDE encryption is an option (OQ-10). | `SELECT banner_full FROM v$version;` |
| 10 | **Password expiry policy** | The default profile expires passwords after 180 days, which would silently break the app's connection. | Ask the DBA for a service-account profile without expiry, or plan rotation. |

Send me the results of items 7, 8 and 9 once you have them, because they can change the design.
Items 2–6 are only needed to connect.

## 3. Make sure you're in the PDB

Connect with your admin account to the **PDB service name**, then:

```sql
SELECT sys_context('USERENV','CON_NAME') AS container FROM dual;
```

It must **not** return `CDB$ROOT`. If it does, you're in the root container, and `CREATE USER`
will fail with `ORA-65096` (root only allows "common" users prefixed with `C##`, which we don't want).

## 4. Create the dev and test schemas

Run as an account with `CREATE USER`, connected to the PDB (SQL Developer and SQLcl both run on macOS; SQL*Plus also works).
Choose your own passwords and store them only in `server/.env`, never in the repo.
Oracle 19c passwords are limited to 30 bytes; the double quotes allow special characters, and the
password is case-sensitive.

```sql
-- Development schema
CREATE USER dropshare_dev IDENTIFIED BY "<dev-password>"
  DEFAULT TABLESPACE users
  TEMPORARY TABLESPACE temp
  QUOTA 10G ON users;

GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE VIEW TO dropshare_dev;

-- Test schema (automated tests create and clean data here; never point it at real data)
CREATE USER dropshare_test IDENTIFIED BY "<test-password>"
  DEFAULT TABLESPACE users
  TEMPORARY TABLESPACE temp
  QUOTA 5G ON users;

GRANT CREATE SESSION, CREATE TABLE, CREATE SEQUENCE, CREATE VIEW TO dropshare_test;
```

What each privilege is for:
- `CREATE SESSION`: connect.
- `CREATE TABLE`: create tables and their indexes in its own schema.
- `CREATE SEQUENCE`: identity columns (used for the change journal and audit log) rely on sequences.
- `CREATE VIEW`: reporting views if we need them.

No `DBA` role and no `ANY` privileges: each account can only touch its own schema. Adjust the
quotas (10G / 5G) if your DBA prefers; file content is not stored in Oracle, so the database stays small.

Unquoted user names are stored in upper case, so they appear as `DROPSHARE_DEV` and `DROPSHARE_TEST`.

## 5. Verify the new schema

Connect **as `dropshare_dev`** to the PDB service and run:

```sql
SELECT user, sys_context('USERENV','CON_NAME') AS container FROM dual;

CREATE TABLE ds_probe (id NUMBER GENERATED ALWAYS AS IDENTITY, name VARCHAR2(50));
INSERT INTO ds_probe (name) VALUES ('ok – ünïcødé ✓');
SELECT * FROM ds_probe;          -- the text must come back exactly as inserted
DROP TABLE ds_probe PURGE;
```

Repeat for `dropshare_test`. If the special characters come back as `?` or garbled, check item 7
(and your SQL tool's own encoding setting) before going further.

## 6. Configure the server

Once the project is scaffolded, put the values in `server/.env` (never committed; placeholders live
in `server/.env.example`):

```dotenv
DB_HOST=<host-ip>
DB_PORT=1521
DB_SERVICE_NAME=<pdb-service-name>
DB_USER=DROPSHARE_DEV
DB_PASSWORD=<dev-password>

TEST_DB_HOST=<host-ip>
TEST_DB_PORT=1521
TEST_DB_SERVICE_NAME=<pdb-service-name>
TEST_DB_USER=DROPSHARE_TEST
TEST_DB_PASSWORD=<test-password>
```

Then `uv run alembic -c server/alembic.ini upgrade head` creates the tables in the dev schema.
The first time, Claude Code will ask for approval before running it.

If your DBA only gives you a **SID** instead of a service name, tell Claude Code; python-oracledb
supports SIDs, but the connection settings change slightly.

## 7. Production (later, at deployment)

Production uses **two** accounts instead of one:
- `DROPSHARE_OWNER` owns the tables and is used only to run migrations.
- `DROPSHARE_APP` is used by the running server. It has only `SELECT/INSERT/UPDATE/DELETE` on the owner's tables, and only `SELECT, INSERT` on the audit table, so audit records can't be altered (FR-AUDIT-03).

The grant script for this will be written as part of the deployment work, together with backup
(RMAN) and password-profile settings agreed with the DBA.

## 8. Troubleshooting

| Error | Usual cause | Fix |
|---|---|---|
| `ORA-12514` (listener does not know of service) | Wrong service name, a SID used as a service name, or the CDB service | Get the PDB service name (item 3). |
| `ORA-12541` / connection timeout | Wrong port, listener down, or firewall | Item 4; ask network/DBA team. |
| `ORA-01017` (invalid username/password) | Wrong password (case-sensitive) or wrong PDB | Check the password and service name. |
| `ORA-65096` (invalid common user or role name) | Running `CREATE USER` in `CDB$ROOT` | Connect to the PDB (section 3). |
| `ORA-01950` (no privileges on tablespace) | Missing `QUOTA` | `ALTER USER dropshare_dev QUOTA 10G ON users;` |
| `ORA-01031` (insufficient privileges) | Missing grant | Re-run the `GRANT` from section 4. |
| `ORA-28001` (password has expired) | Default profile expiry | Reset the password; ask the DBA about a service profile (item 10). |
| `DPY-3001` (Native Network Encryption only supported in thick mode) | Server requires Native Network Encryption | See item 8. |
