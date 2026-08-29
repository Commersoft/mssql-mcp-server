"""repl_apply_pending — kolejka replikacji konfiguracji u klienta (csReplConfigChangesClientLog).

Mechanizm (patrz .github/instructions/autoupdate-client-changes-queue.instructions.md):
  DEV loguje zmiany -> csGenDailyChangeJob tworzy ramkę -> u klienta ...ImportJob wstawia wiersze
  do csReplConfigChangesClientLog (Status 0) -> ...ApplyJob instaluje je po LogId
  (Status 1 = OK, -1 = błąd, który ZATRZYMUJE kolejkę; blockFurtherRows = 1 = twarda blokada).
  Gdy ApplyJob w csCompaniesJobs ma Active = 0 (Grodno PROD od 01.2026) backlog rośnie do czasu
  ręcznego uruchomienia. Ręczny start = csReplConfigChangesClientLogApplyBackground: tworzy job
  msdb `[csSysJobReponseHandle] <csUsrId>_csReplConfigChangesClientLogApplyBackground_<G>`
  (@delete_level = 1 -> kasuje się tylko po sukcesie), który woła ApplyJob i csSysJobReponseHandle.

Pułapki, które to narzędzie egzekwuje:
  - ApplyBackground bierze użytkownika z csSysFnUsrId() = session_context('csUsrId'); bez
    sp_set_session_context w TEJ SAMEJ sesji @csUsrId = NULL -> concat() gubi wartość i krok
    joba pada „Incorrect syntax near ','” (job zostaje w msdb, nic się nie instaluje).
  - kolumny xml (ProcessError, response) konwertuj do nvarchar(max) — convert do nvarchar(N)
    rzuca „Target string size is too small to represent the XML instance”.
  - drugi równoległy ApplyBackground = wyścig na tej samej kolejce -> odmowa startu, gdy job
    o tej nazwie jeszcze biegnie w msdb.
  - postęp: csReplConfigChangesClientLogExecution (1 wiersz na próbę instalacji LogId,
    StopDate NULL = w toku) + msdb.dbo.sysjobactivity / sysjobhistory (nieudany job nie znika).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pyodbc import connect

from ._core import _exec_scalar

_APPLY_BG_PROC = "csReplConfigChangesClientLogApplyBackground"
_JOB_NAME_LIKE = f"%_{_APPLY_BG_PROC}_%"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fmt_dt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)


def _short(v, n: int = 400) -> str:
    s = "" if v is None else str(v).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def _queue_summary(cur) -> List[tuple]:
    return cur.execute(
        "select l.Status, count(*) cnt, min(l.LogDate) oldest, max(l.LogDate) newest, "
        "  sum(convert(int, l.blockFurtherRows)) blocking "
        "from dbo.csReplConfigChangesClientLog l with(nolock) "
        "group by l.Status order by l.Status"
    ).fetchall()


def _pending_count(cur) -> int:
    return int(_exec_scalar(
        cur,
        "select count(*) from dbo.csReplConfigChangesClientLog l with(nolock) where l.Status in (-1, 0)",
    ) or 0)


def _error_rows(cur, top: int = 5) -> List[tuple]:
    return cur.execute(
        "select top (?) l.LogId, l.LogDate, l.Opr, l.ObjectName, l.RowG, l.blockFurtherRows, "
        "  convert(nvarchar(max), l.ProcessError) ProcessError "
        "from dbo.csReplConfigChangesClientLog l with(nolock) "
        "where l.Status = -1 order by l.LogId",
        top,
    ).fetchall()


def _blocking_rows(cur) -> List[tuple]:
    return cur.execute(
        "select l.LogId, l.LogDate, l.Opr, l.ObjectName, l.Status "
        "from dbo.csReplConfigChangesClientLog l with(nolock) "
        "where l.blockFurtherRows = 1 order by l.LogId"
    ).fetchall()


def _client_jobs(cur) -> List[tuple]:
    return cur.execute(
        "select j.csCompaniesId, j.ProcedureName, j.Active, j.LastInvokeTime, j.LastSuccessRunTime, j.Interval "
        "from dbo.csCompaniesJobs j with(nolock) "
        "where j.ProcedureName like N'%csReplConfigChangesClientLog%' order by j.ProcedureName"
    ).fetchall()


def _msdb_jobs(cur, top: int = 5) -> Optional[List[tuple]]:
    """Jobs created by ApplyBackground (running or lingering after failure). None = no msdb access."""
    try:
        return cur.execute(
            "select top (?) j.name, j.date_created, ja.start_execution_date, ja.stop_execution_date, "
            "  ja.last_executed_step_id, h.run_status, h.message "
            "from msdb.dbo.sysjobs j with(nolock) "
            "outer apply (select top 1 a.start_execution_date, a.stop_execution_date, a.last_executed_step_id "
            "  from msdb.dbo.sysjobactivity a with(nolock) where a.job_id = j.job_id "
            "  order by a.session_id desc) ja "
            "outer apply (select top 1 x.run_status, x.message from msdb.dbo.sysjobhistory x with(nolock) "
            "  where x.job_id = j.job_id and x.step_id = 0 "
            "  order by x.instance_id desc) h "
            "where j.name like ? order by j.date_created desc",
            top, _JOB_NAME_LIKE,
        ).fetchall()
    except Exception:  # noqa: BLE001 — brak uprawnień do msdb na tym profilu
        return None


def _running_msdb_job(msdb_rows) -> Optional[str]:
    if not msdb_rows:
        return None
    for name, _created, start, stop, _step, _rs, _msg in msdb_rows:
        if start is not None and stop is None:
            return name
    return None


def _render_queue(rows) -> List[str]:
    out = ["KOLEJKA csReplConfigChangesClientLog (Status | cnt | oldest | newest | blockFurtherRows):"]
    if not rows:
        out.append("  (pusta)")
    names = {-1: "błąd (zatrzymuje kolejkę)", 0: "do instalacji", 1: "zainstalowane"}
    for st, cnt, oldest, newest, blocking in rows:
        out.append(f"  {st:>2} {names.get(st, '?'):<26} | {cnt:>9} | {_fmt_dt(oldest)} | {_fmt_dt(newest)} | blocking={blocking or 0}")
    return out


def _render_errors(rows) -> List[str]:
    if not rows:
        return []
    out = ["BŁĘDY (Status = -1) — ApplyJob ponowi je w kolejności LogId; powtórka błędu = kolejka stoi:"]
    for log_id, log_date, opr, obj, row_g, block, err in rows:
        out.append(f"  LogId {log_id} | {_fmt_dt(log_date)} | {opr} {obj} | RowG {row_g} | blockFurtherRows={block}")
        out.append(f"    ProcessError: {_short(err)}")
    return out


def _render_jobs(rows) -> List[str]:
    out = ["JOBY csCompaniesJobs (*ReplConfigChangesClientLog*):"]
    if not rows:
        out.append("  (brak — to nie jest instalacja kliencka?)")
    for cid, proc, active, last_inv, last_ok, interval in rows:
        flag = ""
        if not active and "ApplyJob" in proc:
            flag = "   <-- Active=0: kolejka NIE instaluje się sama (backlog rośnie do ręcznego startu)"
        elif not active and "ImportJob" in proc:
            flag = "   <-- Active=0: ramki z DEV NIE trafiają do kolejki"
        out.append(f"  firma {cid} | {proc} | Active={int(active)} | LastInvoke {_fmt_dt(last_inv)} | LastSuccess {_fmt_dt(last_ok)} | Interval {interval}s{flag}")
    return out


def _render_msdb(rows) -> List[str]:
    if rows is None:
        return ["JOBY msdb: brak dostępu do msdb na tym profilu (pomiń)."]
    if not rows:
        return ["JOBY msdb ApplyBackground: brak (job kasuje się sam po sukcesie; nieudany zostaje)."]
    out = ["JOBY msdb ApplyBackground (name | created | start | stop | run_status | message):"]
    for name, created, start, stop, _step, rs, msg in rows:
        state = "W TOKU" if (start is not None and stop is None) else ("zakończony" if stop else "nie wystartował")
        out.append(f"  {name} | {_fmt_dt(created)} | {_fmt_dt(start)} | {_fmt_dt(stop)} | {state} | run_status={rs} | {_short(msg, 200)}")
    out.append("  (nieudany job NIE kasuje się sam — @delete_level=1 tylko przy sukcesie; sprzątanie: msdb.dbo.sp_delete_job @job_name=N'…')")
    return out


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def _status(cur, object_like: Optional[str], top: int) -> str:
    lines: List[str] = []
    lines += _render_queue(_queue_summary(cur))
    lines += _render_errors(_error_rows(cur))

    blocking = _blocking_rows(cur)
    if blocking:
        lines.append("BLOKADY (blockFurtherRows = 1 — ApplyJob nie przejdzie poniżej tego LogId):")
        for log_id, log_date, opr, obj, st in blocking:
            lines.append(f"  LogId {log_id} | {_fmt_dt(log_date)} | {opr} {obj} | Status {st}")

    # pending breakdown
    pend = cur.execute(
        "select l.Opr, count(*) cnt from dbo.csReplConfigChangesClientLog l with(nolock) "
        "where l.Status in (-1, 0) group by l.Opr order by cnt desc"
    ).fetchall()
    if pend:
        lines.append("ZALEGŁE wg Opr: " + ", ".join(f"{opr}={cnt}" for opr, cnt in pend)
                     + "   (V=wersja obiektu, I/U/D=wiersz konfiguracji, E=exec: SynchronizeTable/RebuildManagedTable/RunChangeLocally)")
        objs = cur.execute(
            "select top 10 l.ObjectName, count(*) cnt from dbo.csReplConfigChangesClientLog l with(nolock) "
            "where l.Status in (-1, 0) group by l.ObjectName order by cnt desc"
        ).fetchall()
        lines.append("ZALEGŁE wg obiektu (top 10): " + ", ".join(f"{o}={c}" for o, c in objs))

    where_like = ""
    params: list = [top]
    if object_like:
        where_like = " and l.ObjectName like ?"
        params.append(f"%{object_like}%")
    sample = cur.execute(
        "select top (?) l.LogId, l.LogDate, l.Opr, l.ObjectName, l.Status, l.ChangeDesc "
        f"from dbo.csReplConfigChangesClientLog l with(nolock) where l.Status in (-1, 0){where_like} "
        "order by l.LogId",
        *params,
    ).fetchall()
    if sample:
        title = f"PIERWSZE {len(sample)} zaległe po LogId" + (f" (ObjectName like '%{object_like}%')" if object_like else "") + ":"
        lines.append(title)
        for log_id, log_date, opr, obj, st, desc in sample:
            lines.append(f"  {log_id} | {_fmt_dt(log_date)} | {st:>2} | {opr} {obj} | {_short(desc, 80)}")
    elif object_like:
        lines.append(f"Brak zaległych wierszy z ObjectName like '%{object_like}%'.")

    lines += _render_jobs(_client_jobs(cur))
    lines += _render_msdb(_msdb_jobs(cur))

    last = cur.execute(
        "select top 3 e.LogId, e._ordid, e.StartDate, e.StopDate, e.Status, e.Login "
        "from dbo.csReplConfigChangesClientLogExecution e with(nolock) order by e._ordid desc"
    ).fetchall()
    if last:
        lines.append("OSTATNIE PRÓBY INSTALACJI (csReplConfigChangesClientLogExecution):")
        for log_id, ordid, start, stop, st, login in last:
            lines.append(f"  _ordid {ordid} | LogId {log_id} | {_fmt_dt(start)} -> {_fmt_dt(stop)} | Status {st} | {login}")

    pending = _pending_count(cur)
    if pending:
        lines.append(f"=> {pending} pozycji czeka. Start w tle: repl_apply_pending(action='start', server=…) — "
                     "najpierw usuń przyczynę błędów -1 (ApplyJob ponawia je jako pierwsze).")
    else:
        lines.append("=> kolejka czysta (0 pozycji ze Status -1/0).")
    return "\n".join(lines)


def _progress(cur, since_minutes: int) -> str:
    lines: List[str] = []
    msdb_rows = _msdb_jobs(cur)
    lines += _render_msdb(msdb_rows)

    agg = cur.execute(
        "select count(*) cnt, "
        "  sum(iif(e.Status = 1, 1, 0)) ok, sum(iif(e.Status = -1, 1, 0)) err, sum(iif(e.StopDate is null, 1, 0)) running, "
        "  min(e.StartDate) first_start, max(isnull(e.StopDate, e.StartDate)) last_stop "
        "from dbo.csReplConfigChangesClientLogExecution e with(nolock) "
        "where e.StartDate >= dateadd(minute, -?, getdate())",
        since_minutes,
    ).fetchone()
    cnt, ok, err, running, first_start, last_stop = agg
    lines.append(f"PRÓBY INSTALACJI w ostatnich {since_minutes} min: {cnt} (OK {ok or 0}, błąd {err or 0}, w toku {running or 0}) | "
                 f"{_fmt_dt(first_start)} -> {_fmt_dt(last_stop)}")
    # tempo: z okna since_minutes gdy jest w nim realna seria (>= 20 prób), inaczej z 200 ostatnich prób
    # (ten drugi pomiar obejmuje przestoje między seriami — zaniża)
    rate = None
    if (cnt or 0) >= 20 and first_start and last_stop and last_stop > first_start:
        secs = (last_stop - first_start).total_seconds()
        if secs > 0:
            rate = (ok or 0) / secs
            lines.append(f"  tempo w oknie: {rate:.1f} wierszy/s")
    else:
        burst = cur.execute(
            "select count(*) cnt, min(x.StartDate) s, max(x.StopDate) e from ("
            "  select top 200 e.StartDate, e.StopDate from dbo.csReplConfigChangesClientLogExecution e with(nolock) "
            "  where e.StopDate is not null order by e._ordid desc) x"
        ).fetchone()
        if burst and burst[0] and burst[1] and burst[2] and burst[2] > burst[1]:
            secs = (burst[2] - burst[1]).total_seconds()
            if secs > 0:
                rate = burst[0] / secs
                lines.append(f"  tempo ostatnich {burst[0]} prób: {rate:.1f} wierszy/s ({_fmt_dt(burst[1])} -> {_fmt_dt(burst[2])}; "
                             "obejmuje przestoje między seriami — referencja Grodno 29.08: ~18 w/s w ciągłej serii)")

    # wiersz w toku = StopDate NULL i start w oknie; starsze StopDate NULL to sieroty po padniętej sesji
    cur_row = cur.execute(
        "select top 1 e.LogId, e._ordid, e.StartDate, e.Login, l.Opr, l.ObjectName, "
        "  iif(e.StartDate >= dateadd(minute, -?, getdate()), 1, 0) fresh "
        "from dbo.csReplConfigChangesClientLogExecution e with(nolock) "
        "left join dbo.csReplConfigChangesClientLog l with(nolock) on l.LogId = e.LogId "
        "where e.StopDate is null order by e._ordid desc",
        since_minutes,
    ).fetchone()
    if cur_row:
        log_id, ordid, start, login, opr, obj, fresh = cur_row
        if fresh:
            lines.append(f"  TERAZ: LogId {log_id} ({opr} {obj}) od {_fmt_dt(start)} | _ordid {ordid} | {login}")
        else:
            lines.append(f"  (osierocona próba bez StopDate: LogId {log_id} {opr} {obj} od {_fmt_dt(start)} | {login} — "
                         "sesja padła bez domknięcia; NIE oznacza pracy w toku)")

    last = cur.execute(
        "select top 5 e.LogId, e._ordid, e.StartDate, e.StopDate, e.Status, l.Opr, l.ObjectName, "
        "  convert(nvarchar(max), e.response) response "
        "from dbo.csReplConfigChangesClientLogExecution e with(nolock) "
        "left join dbo.csReplConfigChangesClientLog l with(nolock) on l.LogId = e.LogId "
        "order by e._ordid desc"
    ).fetchall()
    if last:
        lines.append("  ostatnie 5 prób:")
        for log_id, ordid, start, stop, st, opr, obj, resp in last:
            extra = f" | {_short(resp, 200)}" if resp else ""
            lines.append(f"    _ordid {ordid} | LogId {log_id} | {opr} {obj} | {_fmt_dt(start)} -> {_fmt_dt(stop)} | Status {st}{extra}")

    lines += _render_queue(_queue_summary(cur))
    lines += _render_errors(_error_rows(cur))
    pending = _pending_count(cur)
    if pending and rate:
        lines.append(f"=> zostało {pending} pozycji, przy {rate:.1f} w/s ≈ {pending / rate / 60:.1f} min.")
    elif pending:
        lines.append(f"=> zostało {pending} pozycji.")
    else:
        lines.append("=> kolejka czysta (0 pozycji ze Status -1/0).")
    running_job = _running_msdb_job(msdb_rows)
    if running_job is None and pending and not (running or 0):
        lines.append("   UWAGA: nic nie biegnie (brak joba msdb w toku, brak próby ze StopDate NULL) — "
                     "jeśli są błędy -1, napraw przyczynę i uruchom ponownie action='start'.")
    if running_job is not None and not pending and not (running or 0):
        lines.append("   UWAGA: job msdb 'W TOKU', a kolejka czysta i żadna próba nie trwa = instalacja SKOŃCZONA; job siedzi "
                     "w epilogu csSysJobReponseHandle -> csChatterSendMessageFromSystem -> csNGAPISendMessage "
                     "(csFnCallWebServiceEnvelope do messagesServerURL, timeout 15 s — na kopii testowej TESTGRODNO trwało to "
                     "3-5 min, po czym job skończył się sukcesem i skasował). Poczekaj; kill sesji 'SQLAgent - TSQL JobStep' + "
                     "msdb.dbo.sp_delete_job tylko gdy trwa znacznie dłużej. URL: dbo.csFnGetAppCharParam(N'messagesServerURL').")
    return "\n".join(lines)


def _start(
    cur,
    usr_login: Optional[str],
    cs_usr_id: Optional[int],
    cs_companies_id: Optional[int],
    dry_run: bool,
    target_label: str,
) -> str:
    lines: List[str] = []

    # 1. user -> csUsrId (session context is what ApplyBackground reads via csSysFnUsrId)
    if cs_usr_id:
        row = cur.execute(
            "select u.csUsrId, u.Login from dbo.csUsr u with(nolock) where u.csUsrId = ?", int(cs_usr_id)
        ).fetchone()
        if not row:
            return f"Error: csUsr {cs_usr_id} nie istnieje na {target_label}."
    else:
        login = (usr_login or "").strip()
        if not login:
            return "Error: podaj usr_login (np. 'jmk') albo cs_usr_id — ApplyBackground potrzebuje csUsrId w session_context."
        row = cur.execute(
            "select u.csUsrId, u.Login from dbo.csUsr u with(nolock) where u.Login = ?", login
        ).fetchone()
        if not row:
            return f"Error: login '{login}' nie istnieje w csUsr na {target_label}."
    uid, login = int(row[0]), row[1]

    # 2. company: explicit or the single company owning the client-side ApplyJob/ImportJob
    #    (ExportJob/Rebuild*Job exist also on the SOURCE — DEV — and do not mark a client install)
    jobs = _client_jobs(cur)
    if cs_companies_id is None:
        companies = sorted({int(j[0]) for j in jobs if "ApplyJob" in j[1] or "ImportJob" in j[1]})
        if len(companies) == 1:
            cs_companies_id = companies[0]
        elif not companies:
            return ("Error: brak jobów ...ClientLogApplyJob/ImportJob w csCompaniesJobs — to nie wygląda na "
                    "instalację kliencką (na DEV kolejka klienta nie ma zastosowania). Podaj cs_companies_id jawnie, jeśli wiesz co robisz.")
        else:
            return f"Error: kilka firm z jobami kolejki ({companies}) — podaj cs_companies_id."
    cs_companies_id = int(cs_companies_id)

    # 3. guards
    pending = _pending_count(cur)
    errors = _error_rows(cur, top=3)
    blocking = _blocking_rows(cur)
    msdb_rows = _msdb_jobs(cur)
    running = _running_msdb_job(msdb_rows)

    lines.append(f"cel: {target_label} | csUsrId {uid} ({login}) | csCompaniesId {cs_companies_id} | zaległe (Status -1/0): {pending}")
    if not pending:
        lines.append("Nic do zrobienia — kolejka czysta. Nie startuję.")
        return "\n".join(lines)
    if running:
        lines.append(f"ODMOWA: job '{running}' jeszcze biegnie w msdb — drugi ApplyBackground na tej samej kolejce to wyścig. "
                     "Sprawdź action='progress'.")
        return "\n".join(lines)
    if blocking:
        lines.append("UWAGA: blockFurtherRows=1 na LogId " + ", ".join(str(b[0]) for b in blocking)
                     + " — ApplyJob zatrzyma się na tej pozycji (wiersze poniżej nie wejdą).")
    if errors:
        lines += _render_errors(errors)
        lines.append("  ApplyJob spróbuje ich PONOWNIE jako pierwszych; jeśli przyczyna nie została usunięta, "
                     "zatrzyma się na tym samym LogId (Status -1 pozostaje, reszta czeka).")
    if msdb_rows:
        stale = [r[0] for r in msdb_rows if not (r[2] is not None and r[3] is None)]
        if stale:
            lines.append(f"  w msdb wiszą stare joby ApplyBackground ({len(stale)}) — nie przeszkadzają, ale posprzątaj sp_delete_job.")

    if dry_run:
        lines.append("DRY RUN — nic nie uruchomiono. Bez dry_run wykona się:")
        lines.append(f"  exec sys.sp_set_session_context @key=N'csUsrId', @value={uid};")
        lines.append(f"  exec dbo.{_APPLY_BG_PROC} @csCompaniesId={cs_companies_id}, @csSupLangId=null, @csUsrId={uid}, @response=@resp out;")
        return "\n".join(lines)

    # 4. start — ONE batch, ONE session (session_context must be visible to csSysFnUsrId inside the proc)
    before = _exec_scalar(cur, "select isnull(max(e._ordid), 0) from dbo.csReplConfigChangesClientLogExecution e with(nolock)")
    # sp_set_session_context @value is sql_variant — bind through declared bigint variables
    cur.execute(
        "set nocount on;\n"
        "declare @resp xml, @stat int, @uid bigint = ?, @cid bigint = ?;\n"
        "exec sys.sp_set_session_context @key = N'csUsrId', @value = @uid;\n"
        f"exec @stat = dbo.{_APPLY_BG_PROC} @csCompaniesId = @cid, @csSupLangId = null, @csUsrId = @uid, @response = @resp out;\n"
        "select @stat stat, convert(nvarchar(max), @resp) resp, "
        "  try_convert(bigint, session_context(N'csUsrId')) ctx_usr;",
        uid, cs_companies_id,
    )
    row = None
    for _ in range(10):  # skip any result sets emitted by msdb procs before our final select
        if cur.description and len(cur.description) == 3 and cur.description[0][0] == "stat":
            row = cur.fetchone()
            break
        if not cur.nextset():
            break
    stat, resp, ctx_usr = (row[0], row[1], row[2]) if row else (None, None, None)
    lines.append(f"{_APPLY_BG_PROC}: stat={stat} | session_context csUsrId={ctx_usr} | response: {_short(resp, 300)}")
    if stat is not None and int(stat) < 0:
        lines.append("START NIEUDANY (stat < 0) — patrz response.")
        return "\n".join(lines)

    # 5. locate the msdb job just created
    job = None
    try:
        job = cur.execute(
            "select top 1 j.name, j.date_created from msdb.dbo.sysjobs j with(nolock) "
            "where j.name like ? order by j.date_created desc",
            f"%{uid}_{_APPLY_BG_PROC}_%",
        ).fetchone()
    except Exception:  # noqa: BLE001
        pass
    if job:
        lines.append(f"job msdb: {job[0]} (utworzony {_fmt_dt(job[1])})")
    else:
        lines.append("job msdb: nie widać (brak dostępu do msdb albo już skończył i się skasował).")
    lines.append(f"punkt odniesienia postępu: max(_ordid) przed startem = {before}")
    lines.append(f"Postęp: repl_apply_pending(action='progress', server='{target_label}'). Tempo referencyjne (Grodno 29.08): ~18 w/s.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def repl_apply_pending(
    connection_string: str,
    action: str = "status",
    usr_login: Optional[str] = None,
    cs_usr_id: Optional[int] = None,
    cs_companies_id: Optional[int] = None,
    object_like: Optional[str] = None,
    top: int = 20,
    since_minutes: int = 60,
    dry_run: bool = False,
    target_label: str = "DEV",
) -> str:
    """Kolejka replikacji u klienta: status / start (ApplyBackground w tle) / progress."""
    act = (action or "status").strip().lower()
    if act not in ("status", "start", "progress"):
        return f"Error: action must be status|start|progress (got '{action}')."
    with connect(connection_string, autocommit=True) as conn:
        with conn.cursor() as cur:
            if not _exec_scalar(cur, "select object_id(N'dbo.csReplConfigChangesClientLog')"):
                return f"Error: brak tabeli csReplConfigChangesClientLog na {target_label} — to nie jest baza cs* z kolejką klienta."
            if act == "status":
                return _status(cur, object_like, int(top or 20))
            if act == "progress":
                return _progress(cur, int(since_minutes or 60))
            return _start(cur, usr_login, cs_usr_id, cs_companies_id, bool(dry_run), target_label)
