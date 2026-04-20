import argparse
import csv
import io
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime

try:
    from urllib.parse import quote as url_quote
    from urllib.request import Request, urlopen
except ImportError:
    from urllib import quote as url_quote
    from urllib2 import Request, urlopen


QUERY_ID_PATTERN = re.compile(r"([0-9a-fA-F]{16}:[0-9a-fA-F]{16})")
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_HUMAN_REPORT_PATH = "comparison_report.txt"
SAMPLE_VALUE_SEPARATOR = " ||| "

try:
    text_type = unicode  # type: ignore[name-defined]
except NameError:
    text_type = str


def decode_if_bytes(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def to_text(value):
    value = decode_if_bytes(value)
    if value is None:
        return ""
    if isinstance(value, text_type):
        return value
    try:
        return text_type(value)
    except Exception:
        return text_type(str(value))


def write_stderr_line(message):
    line = to_text(message) + u"\n"
    try:
        sys.stderr.write(line)
    except Exception:
        # Python 2 stderr may be bytes-only under some console encodings.
        sys.stderr.write(line.encode("utf-8", "replace"))


def write_stdout_line(message):
    line = to_text(message) + u"\n"
    try:
        sys.stdout.write(line)
    except Exception:
        # Python 2 stdout may be bytes-only under some console encodings.
        sys.stdout.write(line.encode("utf-8", "replace"))

    try:
        sys.stdout.flush()
    except Exception:
        pass


def log_info(message):
    write_stdout_line("INFO [{0}] {1}".format(now_utc_iso(), to_text(message)))


def log_warn(message):
    write_stdout_line("WARN [{0}] {1}".format(now_utc_iso(), to_text(message)))


def preview_command(cmd, max_len=280):
    parts = [to_text(chunk) for chunk in cmd]
    command_text = " ".join(parts)
    if len(command_text) <= max_len:
        return command_text
    return command_text[:max_len] + " ...[truncado]"


def decode_csv_row(raw_row):
    if sys.version_info[0] >= 3:
        return raw_row

    decoded = {}
    for key, value in raw_row.items():
        key_text = to_text(key).lstrip(u"\ufeff")
        if value is None:
            value_text = u""
        else:
            value_text = to_text(value)
        decoded[key_text] = value_text
    return decoded


def quote_ident(name):
    return name.strip()


def split_list(value):
    if not value:
        return []
    return [v.strip() for v in value.split(";") if v.strip()]


def read_text_file(path):
    with io.open(path, "r", encoding="utf-8") as f:
        return f.read()


def now_utc_iso():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_parent_dir(path):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent)


def run_command(cmd, step_name=""):
    result = run_command_timed(cmd, step_name=step_name)
    return result["returncode"], result["stdout"], result["stderr"]


def run_command_timed(cmd, step_name=""):
    started_epoch = time.time()
    started_at_utc = now_utc_iso()
    label = step_name or "command"
    log_info("START {0}: {1}".format(label, preview_command(cmd)))

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise RuntimeError(
            "No se pudo ejecutar el comando del sistema. "
            "Verifica que impala-shell exista y sea ejecutable.\n"
            "Comando: {0}\n"
            "Detalle: {1}".format(" ".join(cmd), exc)
        )

    out, err = proc.communicate()

    ended_epoch = time.time()
    ended_at_utc = now_utc_iso()

    out = decode_if_bytes(out)
    err = decode_if_bytes(err)

    elapsed_sec = ended_epoch - started_epoch
    log_info("END {0}: exit_code={1} elapsed={2:.3f}s".format(label, proc.returncode, elapsed_sec))

    return {
        "cmd": list(cmd),
        "returncode": proc.returncode,
        "stdout": out,
        "stderr": err,
        "started_at_utc": started_at_utc,
        "ended_at_utc": ended_at_utc,
        "elapsed_sec": elapsed_sec,
    }


def run_impala_file(sql_file, impala_shell, impala_opts, step_name="", delimited=False):
    cmd = [impala_shell]
    if impala_opts:
        cmd.extend(shlex.split(impala_opts))
    if delimited:
        cmd.extend(["-B", "--quiet"])
    cmd.extend(["-f", sql_file])
    label = step_name or "impala_file:{0}".format(sql_file)
    return run_command(cmd, step_name=label)


def run_impala_file_timed(sql_file, impala_shell, impala_opts, show_profiles=False, step_name="", delimited=False):
    cmd = [impala_shell]
    if impala_opts:
        cmd.extend(shlex.split(impala_opts))
    if delimited:
        cmd.extend(["-B", "--quiet"])
    if show_profiles:
        cmd.append("--show_profiles")
    cmd.extend(["-f", sql_file])
    label = step_name or "impala_file:{0}".format(sql_file)
    return run_command_timed(cmd, step_name=label)


def run_impala_query_timed(query, impala_shell, impala_opts, show_profiles=False, step_name="", delimited=False):
    cmd = [impala_shell]
    if impala_opts:
        cmd.extend(shlex.split(impala_opts))
    if delimited:
        cmd.extend(["-B", "--quiet"])
    if show_profiles:
        cmd.append("--show_profiles")
    cmd.extend(["-q", query])
    default_step = "impala_query"
    if query:
        compact = re.sub(r"\s+", " ", query).strip()
        if compact:
            default_step = "impala_query:{0}".format(compact[:60])
    label = step_name or default_step
    return run_command_timed(cmd, step_name=label)


def resolve_impala_shell_command(command):
    raw = (command or "").strip()
    if not raw:
        raise ValueError("Debes indicar --impala-shell con un comando valido.")

    def candidates_from_raw(base):
        options = [base]
        if os.name == "nt":
            lowered = base.lower()
            if not (lowered.endswith(".exe") or lowered.endswith(".cmd") or lowered.endswith(".bat")):
                options.extend([base + ".cmd", base + ".bat", base + ".exe"])
        return options

    has_path = bool(os.path.dirname(raw)) or os.path.isabs(raw)
    if has_path:
        for candidate in candidates_from_raw(raw):
            if os.path.isfile(candidate):
                return candidate
        raise RuntimeError(
            "No se encontro impala-shell en la ruta indicada: {0}. "
            "Usa --impala-shell con la ruta completa del ejecutable, por ejemplo "
            "C:/ruta/a/impala-shell.cmd".format(raw)
        )

    for candidate in candidates_from_raw(raw):
        if sys.version_info[0] >= 3:
            resolved = shutil.which(candidate)
        else:
            try:
                from distutils.spawn import find_executable
                resolved = find_executable(candidate)
            except ImportError:
                resolved = None
        if resolved:
            return resolved

    raise RuntimeError(
        "No se encontro el comando '{0}' en PATH. "
        "Agrega impala-shell al PATH o indica la ruta completa con --impala-shell.".format(raw)
    )


def open_csv_reader(path):
    if sys.version_info[0] < 3:
        return open(path, "rb")
    return open(path, "r", encoding="utf-8", newline="")


def write_text_file(path, text):
    text = decode_if_bytes(text)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)


def normalize_col_name(col):
    return col.strip().lower()


def sanitize_identifier(raw):
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", raw.strip().lower())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        cleaned = "tmp"
    if cleaned[0].isdigit():
        cleaned = "t_" + cleaned
    return cleaned


def normalize_query_id(value):
    return to_text(value).strip().lower()


def extract_query_id(text):
    if not text:
        return ""

    for line in text.splitlines():
        lowered = line.lower()
        if "query" in lowered and "id" in lowered:
            match = QUERY_ID_PATTERN.search(line)
            if match:
                return normalize_query_id(match.group(1))

    match = QUERY_ID_PATTERN.search(text)
    if match:
        return normalize_query_id(match.group(1))
    return ""


def infer_impala_web_url(impala_opts):
    if not impala_opts:
        return ""

    try:
        tokens = shlex.split(impala_opts)
    except ValueError:
        return ""

    host_part = ""
    for idx, token in enumerate(tokens):
        if token == "-i" and idx + 1 < len(tokens):
            host_part = tokens[idx + 1]
            break
        if token.startswith("--impalad="):
            host_part = token.split("=", 1)[1].strip()
            break

    if not host_part:
        return ""

    host = host_part.split(":", 1)[0].strip()
    if not host:
        return ""
    return "http://{0}:25000".format(host)


def parse_float(value):
    try:
        return float(str(value).strip())
    except Exception:
        return None


def parse_size_to_bytes(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    if not text:
        return None

    text = text.replace(",", "")

    # Examples: 1234, 10MB, 10 MB, 1.25 GB, 150bytes
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgtp]?)(?:i?b|bytes?)?$", text, re.IGNORECASE)
    if not match:
        return None

    number = float(match.group(1))
    unit = match.group(2).lower()
    factors = {
        "": 1,
        "k": 1024,
        "m": 1024 ** 2,
        "g": 1024 ** 3,
        "t": 1024 ** 4,
        "p": 1024 ** 5,
    }
    return int(number * factors.get(unit, 1))


def parse_duration_to_ms(value):
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip().lower()
    if not text:
        return None

    text = text.replace(",", "")

    # HH:MM:SS.sss
    if re.match(r"^\d{1,2}:\d{2}:\d{2}(?:\.\d+)?$", text):
        parts = text.split(":")
        hours = float(parts[0])
        minutes = float(parts[1])
        seconds = float(parts[2])
        return (hours * 3600.0 + minutes * 60.0 + seconds) * 1000.0

    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(ns|us|ms|s|m|h)$", text)
    if not match:
        return None

    number = float(match.group(1))
    unit = match.group(2)
    if unit == "ns":
        return number / 1e6
    if unit == "us":
        return number / 1e3
    if unit == "ms":
        return number
    if unit == "s":
        return number * 1000.0
    if unit == "m":
        return number * 60.0 * 1000.0
    if unit == "h":
        return number * 3600.0 * 1000.0
    return None


def parse_numeric_series(value):
    if value is None:
        return []

    if isinstance(value, (int, float)):
        return [float(value)]

    if isinstance(value, list):
        values = []
        for item in value:
            num = parse_float(item)
            if num is not None:
                values.append(num)
        return values

    text = str(value).strip()
    if not text:
        return []

    chunks = re.split(r"[,;\s]+", text)
    values = []
    for chunk in chunks:
        if not chunk:
            continue
        num = parse_float(chunk)
        if num is not None:
            values.append(num)
    return values


def walk_json(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, dict):
            for value in current.values():
                stack.append(value)
        elif isinstance(current, list):
            for value in current:
                stack.append(value)


def fetch_json(url, timeout_sec):
    started = time.time()
    log_info("START impala_api_request: url={0} timeout={1}s".format(url, timeout_sec))
    request = Request(url)
    request.add_header("Accept", "application/json")
    response = urlopen(request, timeout=timeout_sec)
    try:
        payload = response.read()
    finally:
        if hasattr(response, "close"):
            response.close()

    payload = decode_if_bytes(payload)
    log_info("END impala_api_request: url={0} elapsed={1:.3f}s".format(url, time.time() - started))
    return json.loads(payload)


def pick_first(mapping, keys):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def find_query_entry(payload, query_id):
    target = normalize_query_id(query_id)
    for node in walk_json(payload):
        if not isinstance(node, dict):
            continue

        query_value = None
        for key in ("query_id", "queryId", "queryid", "id", "queryIdStr"):
            if key in node:
                query_value = node[key]
                break

        if query_value is None:
            continue

        if normalize_query_id(query_value) == target:
            return node
    return None


def normalize_cpu_values(values, unit_hint):
    normalized = []
    is_basis_points = "basis" in str(unit_hint).lower()
    for value in values:
        if value is None:
            continue
        cpu_value = float(value)
        if is_basis_points or cpu_value > 100.0:
            cpu_value = cpu_value / 100.0
        normalized.append(cpu_value)
    return normalized


def extract_profile_json_metrics(profile_payload):
    metrics = {
        "cpu_user_pct_avg": None,
        "cpu_sys_pct_avg": None,
        "cpu_iowait_pct_avg": None,
        "peak_memory_bytes": None,
    }

    cpu_user_samples = []
    cpu_sys_samples = []
    cpu_iowait_samples = []
    memory_candidates = []

    for node in walk_json(profile_payload):
        if not isinstance(node, dict):
            continue

        for mem_key in (
            "mem_usage",
            "mem_est",
            "peak_mem",
            "peak_memory",
            "peak_memory_bytes",
            "memory_usage",
            "max_mem",
        ):
            if mem_key in node:
                parsed_mem = parse_size_to_bytes(node[mem_key])
                if parsed_mem is not None:
                    memory_candidates.append(parsed_mem)

        counter_name = node.get("counter_name") or node.get("name")
        if not counter_name:
            continue

        counter_name_lower = str(counter_name).lower()
        unit_hint = str(node.get("unit", ""))

        values = parse_numeric_series(node.get("data"))
        if not values and node.get("value") is not None:
            scalar = parse_float(node.get("value"))
            if scalar is not None:
                values = [scalar]

        if not values:
            continue

        if "hostcpuuserpercentage" in counter_name_lower:
            cpu_user_samples.extend(normalize_cpu_values(values, unit_hint))
            continue
        if "hostcpusyspercentage" in counter_name_lower:
            cpu_sys_samples.extend(normalize_cpu_values(values, unit_hint))
            continue
        if "hostcpuiowaitpercentage" in counter_name_lower:
            cpu_iowait_samples.extend(normalize_cpu_values(values, unit_hint))
            continue

        if "memory" in counter_name_lower:
            unit_lower = unit_hint.lower()
            if "byte" in unit_lower:
                memory_candidates.extend([int(v) for v in values])
            elif max(values) > 1024.0:
                # Many memory counters are already in bytes even when unit metadata is absent.
                memory_candidates.extend([int(v) for v in values])

    if cpu_user_samples:
        metrics["cpu_user_pct_avg"] = sum(cpu_user_samples) / float(len(cpu_user_samples))
    if cpu_sys_samples:
        metrics["cpu_sys_pct_avg"] = sum(cpu_sys_samples) / float(len(cpu_sys_samples))
    if cpu_iowait_samples:
        metrics["cpu_iowait_pct_avg"] = sum(cpu_iowait_samples) / float(len(cpu_iowait_samples))
    if memory_candidates:
        metrics["peak_memory_bytes"] = max(memory_candidates)

    return metrics


def extract_profile_text_metrics(profile_text):
    metrics = {
        "cpu_user_pct_avg": None,
        "cpu_sys_pct_avg": None,
        "cpu_iowait_pct_avg": None,
        "peak_memory_bytes": None,
    }

    if not profile_text:
        return metrics

    def extract_cpu_samples(counter_name):
        regex = re.compile(r"(?i){0}[^0-9\-]*([0-9]+(?:\.[0-9]+)?)".format(re.escape(counter_name)))
        values = [parse_float(x) for x in regex.findall(profile_text)]
        values = [x for x in values if x is not None]
        return normalize_cpu_values(values, "basis_points")

    user_samples = extract_cpu_samples("HostCpuUserPercentage")
    sys_samples = extract_cpu_samples("HostCpuSysPercentage")
    iowait_samples = extract_cpu_samples("HostCpuIoWaitPercentage")

    if user_samples:
        metrics["cpu_user_pct_avg"] = sum(user_samples) / float(len(user_samples))
    if sys_samples:
        metrics["cpu_sys_pct_avg"] = sum(sys_samples) / float(len(sys_samples))
    if iowait_samples:
        metrics["cpu_iowait_pct_avg"] = sum(iowait_samples) / float(len(iowait_samples))

    memory_candidates = []
    for regex in (
        re.compile(r"(?i)(?:peak[^\n]*memory|peakmem[^\n]*)[^0-9]*([0-9]+(?:\.[0-9]+)?\s*[kmgtp]?b)") ,
        re.compile(r"(?i)(?:peak[^\n]*memory|peakmem[^\n]*)[^0-9]*([0-9][0-9,]*)"),
    ):
        for raw_value in regex.findall(profile_text):
            parsed_mem = parse_size_to_bytes(raw_value)
            if parsed_mem is not None:
                memory_candidates.append(parsed_mem)

    if memory_candidates:
        metrics["peak_memory_bytes"] = max(memory_candidates)

    return metrics


def collect_api_metrics(query_id, impala_web_url, timeout_sec):
    metrics = {
        "api_duration_ms": None,
        "peak_memory_bytes": None,
        "cpu_user_pct_avg": None,
        "cpu_sys_pct_avg": None,
        "cpu_iowait_pct_avg": None,
        "sources": [],
        "warnings": [],
    }

    if not impala_web_url or not query_id:
        return metrics

    base_url = impala_web_url.rstrip("/")

    try:
        payload = fetch_json(base_url + "/queries?json", timeout_sec)
        entry = find_query_entry(payload, query_id)
        if entry:
            duration_raw = pick_first(entry, ["duration", "duration_ms", "query_duration", "exec_time", "time_ms"])
            duration_ms = parse_duration_to_ms(duration_raw)
            if duration_ms is not None:
                metrics["api_duration_ms"] = duration_ms

            memory_raw = pick_first(entry, ["mem_usage", "peak_mem", "peak_memory", "memory_usage", "mem_est"])
            memory_bytes = parse_size_to_bytes(memory_raw)
            if memory_bytes is not None:
                metrics["peak_memory_bytes"] = memory_bytes

            metrics["sources"].append("queries_json")
        else:
            metrics["warnings"].append("query_id_no_encontrado_en_queries_json")
    except Exception as exc:
        metrics["warnings"].append("error_queries_json:{0}".format(exc))

    try:
        profile_url = base_url + "/query_profile_json?query_id=" + url_quote(query_id)
        profile_payload = fetch_json(profile_url, timeout_sec)
        profile_metrics = extract_profile_json_metrics(profile_payload)

        for key in ("peak_memory_bytes", "cpu_user_pct_avg", "cpu_sys_pct_avg", "cpu_iowait_pct_avg"):
            if profile_metrics.get(key) is not None:
                metrics[key] = profile_metrics[key]

        metrics["sources"].append("query_profile_json")
    except Exception as exc:
        metrics["warnings"].append("error_query_profile_json:{0}".format(exc))

    return metrics


def load_sql_query_for_ctas(path):
    sql_text = read_text_file(path).strip()
    if not sql_text:
        raise ValueError("El archivo SQL esta vacio: {0}".format(path))

    while sql_text.endswith(";"):
        sql_text = sql_text[:-1].strip()

    if not sql_text:
        raise ValueError("El archivo SQL no contiene una consulta valida: {0}".format(path))

    if ";" in sql_text:
        raise ValueError(
            "El archivo {0} debe contener una unica query SELECT (CTEs permitidas), sin multiples sentencias.".format(path)
        )

    if not re.match(r"^(with|select)\b", sql_text, flags=re.IGNORECASE):
        raise ValueError(
            "El archivo {0} debe iniciar con SELECT o WITH para crear la tabla temporal.".format(path)
        )

    return sql_text


def make_temp_table_name(temp_db, temp_prefix, pair_name, role):
    db = sanitize_identifier(temp_db)
    prefix = sanitize_identifier(temp_prefix)
    pair = sanitize_identifier(pair_name)
    role_part = sanitize_identifier(role)

    base_name = "{0}_{1}_{2}".format(prefix, pair, role_part)
    base_name = base_name[:80]
    suffix = "{0}_{1}_{2}".format(int(time.time()), os.getpid(), random.randint(1000, 9999))
    return "{0}.{1}_{2}".format(db, base_name, suffix)


def format_impala_error(action_label, result):
    stderr = (result.get("stderr") or "").strip()
    stdout = (result.get("stdout") or "").strip()
    cmd = " ".join(result.get("cmd") or [])

    lines = [
        "ERROR en {0} (exit code {1}).".format(action_label, result.get("returncode")),
        "Comando: {0}".format(cmd),
    ]
    if stderr:
        lines.append("STDERR: {0}".format(stderr))
    if stdout:
        lines.append("STDOUT: {0}".format(stdout[:2000]))
    return "\n".join(lines)


def build_step_metrics(step_name, result, impala_web_url, web_timeout_sec, allow_api=True):
    combined_output = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    query_id = extract_query_id(combined_output)

    metrics = {
        "step": step_name,
        "status": "OK" if result.get("returncode") == 0 else "ERROR",
        "return_code": result.get("returncode"),
        "query_id": query_id,
        "started_at_utc": result.get("started_at_utc"),
        "ended_at_utc": result.get("ended_at_utc"),
        "elapsed_wall_sec": result.get("elapsed_sec"),
        "api_duration_ms": None,
        "peak_memory_bytes": None,
        "cpu_user_pct_avg": None,
        "cpu_sys_pct_avg": None,
        "cpu_iowait_pct_avg": None,
        "metric_source": "",
        "metric_warnings": [],
    }

    source_tags = []

    if allow_api and impala_web_url and query_id:
        api_metrics = collect_api_metrics(query_id, impala_web_url, web_timeout_sec)
        for key in ("api_duration_ms", "peak_memory_bytes", "cpu_user_pct_avg", "cpu_sys_pct_avg", "cpu_iowait_pct_avg"):
            if api_metrics.get(key) is not None:
                metrics[key] = api_metrics[key]

        if api_metrics.get("sources"):
            source_tags.extend(api_metrics.get("sources"))
        if api_metrics.get("warnings"):
            metrics["metric_warnings"].extend(api_metrics.get("warnings"))

    needs_profile_fallback = (
        metrics["peak_memory_bytes"] is None
        or metrics["cpu_user_pct_avg"] is None
        or metrics["cpu_sys_pct_avg"] is None
        or metrics["cpu_iowait_pct_avg"] is None
    )

    if needs_profile_fallback and combined_output.strip():
        profile_metrics = extract_profile_text_metrics(combined_output)
        for key in ("peak_memory_bytes", "cpu_user_pct_avg", "cpu_sys_pct_avg", "cpu_iowait_pct_avg"):
            if metrics.get(key) is None and profile_metrics.get(key) is not None:
                metrics[key] = profile_metrics[key]

        if any(profile_metrics.values()):
            source_tags.append("profile_text")

    if not source_tags:
        source_tags.append("wall_clock_only")

    metrics["metric_source"] = ",".join(source_tags)
    return metrics


def format_mb(memory_bytes):
    if memory_bytes is None:
        return "n/a"
    return "{0:.2f}".format(float(memory_bytes) / (1024.0 * 1024.0))


def format_pct(value):
    if value is None:
        return "n/a"
    return "{0:.2f}".format(value)


def print_metrics_summary(metrics_rows):
    if not metrics_rows:
        return

    print("\n=== Metricas por query ===")
    for row in metrics_rows:
        print(
            "[{0}] status={1} elapsed={2:.3f}s query_id={3}".format(
                row["step"],
                row["status"],
                float(row["elapsed_wall_sec"] or 0.0),
                row["query_id"] or "n/a",
            )
        )
        print(
            "  source={0} duration_ms={1} peak_mem_mb={2} cpu_user={3}% cpu_sys={4}% cpu_iowait={5}%".format(
                row["metric_source"],
                "{0:.2f}".format(row["api_duration_ms"]) if row["api_duration_ms"] is not None else "n/a",
                format_mb(row["peak_memory_bytes"]),
                format_pct(row["cpu_user_pct_avg"]),
                format_pct(row["cpu_sys_pct_avg"]),
                format_pct(row["cpu_iowait_pct_avg"]),
            )
        )
        if row.get("metric_warnings"):
            print("  warnings={0}".format(" | ".join(row["metric_warnings"])))


def parse_delimited_result_rows(text, expected_cols=0):
    rows = []
    for raw_line in (text or "").splitlines():
        line = to_text(raw_line).strip()
        if not line:
            continue

        lower = line.lower()
        if line.startswith("#") or line.startswith("+") or line.startswith("|"):
            continue
        if lower.startswith("starting impala shell") or lower.startswith("query:"):
            continue
        if lower.startswith("warning:") or lower.startswith("warn:"):
            continue
        if re.match(r"^fetched\s+\d+\s+row\(s\)", lower):
            continue

        if expected_cols == 1:
            parts = [line]
        elif expected_cols > 1:
            if "\t" not in line:
                continue
            parts = line.split("\t", expected_cols - 1)
            if len(parts) != expected_cols:
                continue
        else:
            if "\t" not in line:
                continue
            parts = line.split("\t")

        rows.append([to_text(part).strip() for part in parts])
    return rows


def split_sample_value(value):
    text = to_text(value)
    if SAMPLE_VALUE_SEPARATOR in text:
        key_text, row_text = text.split(SAMPLE_VALUE_SEPARATOR, 1)
        return key_text.strip(), row_text.strip()
    return text.strip(), ""


def parse_comparison_output(stdout_text):
    metrics_by_pair = {}
    samples_by_pair = {}

    for pair_name, metric, value in parse_delimited_result_rows(stdout_text, expected_cols=3):
        if not pair_name or not metric:
            continue

        metric_key = metric.strip()
        if metric_key in ("SAMPLE_A_ONLY", "SAMPLE_B_ONLY"):
            pair_samples = samples_by_pair.setdefault(pair_name, {"A_ONLY": [], "B_ONLY": []})
            key_text, row_text = split_sample_value(value)
            side = "A_ONLY" if metric_key == "SAMPLE_A_ONLY" else "B_ONLY"
            pair_samples[side].append({"key": key_text, "row": row_text})
            continue

        pair_metrics = metrics_by_pair.setdefault(pair_name, {})
        pair_metrics[metric_key] = value

    return metrics_by_pair, samples_by_pair


def evaluate_step1_results(rows, metrics_by_pair):
    pair_results = []
    all_pass = True

    for row_data in rows:
        pair_name = row_data[0]
        metrics = metrics_by_pair.get(pair_name, {})

        strict_result = to_text(metrics.get("STRICT_100_RESULT", "")).strip().upper()
        fast_result = to_text(metrics.get("FAST_AUDIT_RESULT", "")).strip().upper()

        status = "PASS" if strict_result == "OK" else "FAIL"
        if status != "PASS":
            all_pass = False

        if strict_result:
            reason = "STRICT_100_RESULT={0}".format(strict_result)
        else:
            reason = "No se encontro STRICT_100_RESULT en la salida de comparacion"

        pair_results.append(
            {
                "pair_name": pair_name,
                "status": status,
                "reason": reason,
                "strict_result": strict_result or "NO_DATA",
                "fast_result": fast_result or "NO_DATA",
                "metrics": metrics,
            }
        )

    if not pair_results:
        all_pass = False

    return {
        "status": "PASS" if all_pass else "FAIL",
        "all_pass": all_pass,
        "pairs": pair_results,
        "reason": "",
    }


def truncate_text(value, max_len):
    text = to_text(value)
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3] + "..."


def render_side_by_side_samples(pair_samples, sample_limit):
    a_samples = (pair_samples or {}).get("A_ONLY", [])[:sample_limit]
    b_samples = (pair_samples or {}).get("B_ONLY", [])[:sample_limit]

    lines = []
    if not a_samples and not b_samples:
        lines.append("  No se encontraron muestras de diferencias para mostrar.")
        return lines

    col_width = 72
    lines.append("  Muestras lado a lado (hasta {0} por lado):".format(sample_limit))
    lines.append(
        "  {0:<{w}} | {1:<{w}}".format(
            "ORIGINAL (A_ONLY)",
            "REFACTOR (B_ONLY)",
            w=col_width,
        )
    )
    lines.append("  " + ("-" * col_width) + "-+-" + ("-" * col_width))

    row_total = max(len(a_samples), len(b_samples))
    for idx in range(row_total):
        left_text = ""
        right_text = ""

        if idx < len(a_samples):
            left_sample = a_samples[idx]
            left_key = truncate_text(left_sample.get("key", ""), 28)
            left_row = truncate_text(left_sample.get("row", ""), col_width - 37)
            left_text = "KEY={0} ROW={1}".format(left_key, left_row)

        if idx < len(b_samples):
            right_sample = b_samples[idx]
            right_key = truncate_text(right_sample.get("key", ""), 28)
            right_row = truncate_text(right_sample.get("row", ""), col_width - 37)
            right_text = "KEY={0} ROW={1}".format(right_key, right_row)

        lines.append("  {0:<{w}} | {1:<{w}}".format(left_text, right_text, w=col_width))

    return lines


def to_float_metric(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def format_efficiency_line(label, original_value, refactor_value, formatter, lower_is_better=True):
    original_num = to_float_metric(original_value)
    refactor_num = to_float_metric(refactor_value)

    if original_num is None or refactor_num is None:
        return "{0}: original={1} | refactor={2} | delta=n/a | winner=n/a".format(
            label,
            formatter(original_num),
            formatter(refactor_num),
        )

    delta = refactor_num - original_num
    if original_num == 0:
        delta_pct = "n/a"
    else:
        delta_pct = "{0:+.2f}%".format((delta / original_num) * 100.0)

    if abs(delta) < 1e-12:
        winner = "EMPATE"
    elif lower_is_better:
        winner = "REFACTOR" if delta < 0 else "ORIGINAL"
    else:
        winner = "REFACTOR" if delta > 0 else "ORIGINAL"

    return "{0}: original={1} | refactor={2} | delta={3:+.6f} ({4}) | winner={5}".format(
        label,
        formatter(original_num),
        formatter(refactor_num),
        delta,
        delta_pct,
        winner,
    )


def build_human_report_text(mode_name, step1_summary, samples_by_pair, step2_summary, sample_limit):
    lines = []
    lines.append("VALIDADOR DE QUERIES - REPORTE HUMANO")
    lines.append("Generado: {0}".format(now_utc_iso()))
    lines.append("Modo: {0}".format(mode_name))
    lines.append("")

    lines.append("STEP 1 - EQUIVALENCIA FUNCIONAL (regla: STRICT_100_RESULT = OK)")
    lines.append("=" * 100)

    step1_pairs = step1_summary.get("pairs", [])
    if not step1_pairs:
        lines.append("No se pudieron obtener metricas de comparacion para Step 1.")
    else:
        for pair_result in step1_pairs:
            pair_name = pair_result.get("pair_name", "(sin_nombre)")
            metrics = pair_result.get("metrics") or {}

            lines.append("Par: {0}".format(pair_name))
            lines.append("  Resultado: {0}".format(pair_result.get("status", "FAIL")))
            lines.append("  Razon: {0}".format(pair_result.get("reason", "n/a")))
            lines.append("  FAST_AUDIT_RESULT: {0}".format(pair_result.get("fast_result", "NO_DATA")))
            lines.append("  STRICT_100_RESULT: {0}".format(pair_result.get("strict_result", "NO_DATA")))
            lines.append(
                "  A_total={0} | B_total={1} | A_minus_B_fullrow={2} | B_minus_A_fullrow={3}".format(
                    metrics.get("A_total", "n/a"),
                    metrics.get("B_total", "n/a"),
                    metrics.get("A_minus_B_fullrow", "n/a"),
                    metrics.get("B_minus_A_fullrow", "n/a"),
                )
            )

            if pair_result.get("status") != "PASS":
                pair_samples = samples_by_pair.get(pair_name, {"A_ONLY": [], "B_ONLY": []})
                lines.extend(render_side_by_side_samples(pair_samples, sample_limit))

            lines.append("")

    lines.append("Resultado global Step 1: {0}".format(step1_summary.get("status", "FAIL")))
    lines.append("")

    lines.append("STEP 2 - EFICIENCIA (read-only re-ejecucion de queries)")
    lines.append("=" * 100)
    step2_status = step2_summary.get("status", "SKIPPED")
    lines.append("Estado: {0}".format(step2_status))

    if step2_status != "COMPLETED":
        lines.append("Motivo: {0}".format(step2_summary.get("reason", "n/a")))
        return "\n".join(lines)

    lines.append("Metodo: ejecucion directa de cada query en modo read-only.")
    if step2_summary.get("original_rowcount") or step2_summary.get("refactor_rowcount"):
        lines.append("Rowcount original: {0}".format(step2_summary.get("original_rowcount") or "n/a"))
        lines.append("Rowcount refactor: {0}".format(step2_summary.get("refactor_rowcount") or "n/a"))

    original_metrics = step2_summary.get("original_metrics") or {}
    refactor_metrics = step2_summary.get("refactor_metrics") or {}

    lines.append("Fuente metricas original: {0}".format(original_metrics.get("metric_source", "n/a")))
    lines.append("Fuente metricas refactor: {0}".format(refactor_metrics.get("metric_source", "n/a")))
    lines.append("")

    lines.append(
        format_efficiency_line(
            "Tiempo wall-clock",
            original_metrics.get("elapsed_wall_sec"),
            refactor_metrics.get("elapsed_wall_sec"),
            lambda v: "n/a" if v is None else "{0:.3f}s".format(v),
            lower_is_better=True,
        )
    )
    lines.append(
        format_efficiency_line(
            "Memoria pico",
            original_metrics.get("peak_memory_bytes"),
            refactor_metrics.get("peak_memory_bytes"),
            lambda v: "n/a" if v is None else "{0:.2f}MB".format(v / (1024.0 * 1024.0)),
            lower_is_better=True,
        )
    )
    lines.append(
        format_efficiency_line(
            "CPU user promedio",
            original_metrics.get("cpu_user_pct_avg"),
            refactor_metrics.get("cpu_user_pct_avg"),
            lambda v: "n/a" if v is None else "{0:.2f}%".format(v),
            lower_is_better=True,
        )
    )
    lines.append(
        format_efficiency_line(
            "CPU sys promedio",
            original_metrics.get("cpu_sys_pct_avg"),
            refactor_metrics.get("cpu_sys_pct_avg"),
            lambda v: "n/a" if v is None else "{0:.2f}%".format(v),
            lower_is_better=True,
        )
    )
    lines.append(
        format_efficiency_line(
            "CPU iowait promedio",
            original_metrics.get("cpu_iowait_pct_avg"),
            refactor_metrics.get("cpu_iowait_pct_avg"),
            lambda v: "n/a" if v is None else "{0:.2f}%".format(v),
            lower_is_better=True,
        )
    )

    warnings = []
    warnings.extend(original_metrics.get("metric_warnings") or [])
    warnings.extend(refactor_metrics.get("metric_warnings") or [])
    if warnings:
        lines.append("")
        lines.append("Advertencias de metricas:")
        for warning in warnings:
            lines.append("- {0}".format(warning))

    return "\n".join(lines)


def build_rows_from_pairs_csv(args):
    rows = []
    log_info("Leyendo archivo de pares: {0}".format(args.pairs))
    with open_csv_reader(args.pairs) as f:
        reader = csv.DictReader(f)
        pair_count = 0
        for raw_row in reader:
            row = decode_csv_row(raw_row)
            pair_name = row["pair_name"].strip()
            original_table = quote_ident(row["original_table"])
            refactor_table = quote_ident(row["refactor_table"])
            key_columns = split_list(row.get("key_columns", ""))

            pair_count += 1
            log_info("Procesando par #{0}: {1}".format(pair_count, pair_name or "(sin_nombre)"))

            if not pair_name or not original_table or not refactor_table:
                raise ValueError("Cada fila debe tener pair_name, original_table y refactor_table.")

            cols_a = run_describe(original_table, args.impala_shell, args.impala_opts)
            cols_b = run_describe(refactor_table, args.impala_shell, args.impala_opts)
            common_cols = shared_columns(cols_a, cols_b)
            if not common_cols:
                raise ValueError(
                    "El par {0} no tiene columnas en comun entre {1} y {2}.".format(
                        pair_name, original_table, refactor_table
                    )
                )

            if args.auto_columns and not key_columns:
                key_columns = choose_auto_key_columns(common_cols)

            if not key_columns:
                raise ValueError(
                    "El par {0} no tiene key_columns. Cargalas en el CSV o usa --auto-columns.".format(pair_name)
                )

            rows.append((pair_name, original_table, refactor_table, key_columns, common_cols))

            log_info("Total de pares cargados: {0}".format(len(rows)))

    return rows


def run_describe(table_name, impala_shell, impala_opts):
    cmd = [impala_shell]
    if impala_opts:
        cmd.extend(shlex.split(impala_opts))
    cmd.extend(["-B", "--quiet", "--output_delimiter=,", "-q", "DESCRIBE {0}".format(table_name)])

    returncode, stdout, stderr = run_command(cmd, step_name="describe_table:{0}".format(table_name))
    if returncode != 0:
        raise RuntimeError(
            "No se pudo ejecutar DESCRIBE para {0}.\n"
            "Comando: {1}\n"
            "STDERR: {2}".format(table_name, " ".join(cmd), stderr.strip())
        )

    cols = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        first = line.split(",", 1)[0].strip()
        if not first:
            continue
        if first.startswith("#"):
            continue
        if first.lower().startswith("partition"):
            continue
        if first.startswith("+") or first.startswith("|"):
            continue
        cols.append(first)

    if not cols:
        raise RuntimeError(
            "DESCRIBE no devolvio columnas para {0}. "
            "Verifica permisos, nombre de tabla y conexion de impala-shell.".format(table_name)
        )
    return cols


def shared_columns(cols_a, cols_b):
    b_norm = {normalize_col_name(c): c for c in cols_b}
    common = []
    for c in cols_a:
        key = normalize_col_name(c)
        if key in b_norm:
            common.append(c)
    return common


def choose_auto_key_columns(common_cols):
    preferred_sets = [
        ["id_cuenta", "fecha_saldo"],
        ["id_cliente_core", "id_sucursal", "id_cta", "nro_cta", "fecha_saldo"],
        ["id_cliente_core", "id_sucursal", "id_cta", "nro_cta"],
    ]

    common_norm_map = {normalize_col_name(c): c for c in common_cols}
    for pref in preferred_sets:
        if all(p in common_norm_map for p in pref):
            return [common_norm_map[p] for p in pref]

    key_like = []
    for c in common_cols:
        n = normalize_col_name(c)
        if re.match(r"^(id_|nro_|cod_|codigo_)", n):
            key_like.append(c)
            continue
        if n in ("nro_cta", "nro_cuenta", "fecha_saldo", "fecha_proceso", "cbu", "clave_apareo"):
            key_like.append(c)

    if key_like:
        return key_like

    # Last resort: all common columns.
    return list(common_cols)


def build_key_expr(key_columns):
    parts = ["coalesce(cast({0} as string), '__NULL__')".format(col) for col in key_columns]
    return "concat_ws('|', " + ", ".join(parts) + ")"


def build_row_expr(columns):
    parts = ["coalesce(cast({0} as string), '__NULL__')".format(col) for col in columns]
    return "concat_ws('|', " + ", ".join(parts) + ")"


def metric_block(pair_name, original_table, refactor_table, key_columns, compare_columns, sample_size=DEFAULT_SAMPLE_SIZE):
    a_key = build_key_expr(key_columns)
    b_key = build_key_expr(key_columns)
    a_row = build_row_expr(compare_columns)
    b_row = build_row_expr(compare_columns)

    block = []
    block.append("-- Pair: {0}".format(pair_name))
    block.append("WITH")
    block.append("a AS (SELECT {0} AS __cmp_key, {1} AS __row_text FROM {2}),".format(a_key, a_row, original_table))
    block.append("b AS (SELECT {0} AS __cmp_key, {1} AS __row_text FROM {2}),".format(b_key, b_row, refactor_table))
    block.append("ab_key AS (SELECT cast(count(*) as bigint) AS value FROM a LEFT ANTI JOIN b ON a.__cmp_key = b.__cmp_key),")
    block.append("ba_key AS (SELECT cast(count(*) as bigint) AS value FROM b LEFT ANTI JOIN a ON b.__cmp_key = a.__cmp_key),")
    block.append("a_rows AS (SELECT __row_text, cast(count(*) as bigint) AS cnt FROM a GROUP BY __row_text),")
    block.append("b_rows AS (SELECT __row_text, cast(count(*) as bigint) AS cnt FROM b GROUP BY __row_text),")
    block.append("ab_rows AS (SELECT cast(count(*) as bigint) AS value FROM a_rows LEFT ANTI JOIN b_rows ON a_rows.__row_text = b_rows.__row_text AND a_rows.cnt = b_rows.cnt),")
    block.append("ba_rows AS (SELECT cast(count(*) as bigint) AS value FROM b_rows LEFT ANTI JOIN a_rows ON b_rows.__row_text = a_rows.__row_text AND b_rows.cnt = a_rows.cnt),")
    block.append("a_only_sample AS (SELECT a.__cmp_key, a.__row_text FROM a LEFT ANTI JOIN b ON a.__cmp_key = b.__cmp_key LIMIT {0}),".format(int(sample_size)))
    block.append("b_only_sample AS (SELECT b.__cmp_key, b.__row_text FROM b LEFT ANTI JOIN a ON b.__cmp_key = a.__cmp_key LIMIT {0}),".format(int(sample_size)))
    block.append("a_hash AS (SELECT cast(coalesce(sum(cast(fnv_hash(__row_text) as bigint)), 0) as bigint) AS value FROM a),")
    block.append("b_hash AS (SELECT cast(coalesce(sum(cast(fnv_hash(__row_text) as bigint)), 0) as bigint) AS value FROM b),")
    block.append("a_total AS (SELECT cast(count(*) as bigint) AS value FROM a),")
    block.append("b_total AS (SELECT cast(count(*) as bigint) AS value FROM b)")
    block.append("SELECT '{0}' AS pair_name, 'A_total' AS metric, cast(a_total.value as string) AS value FROM a_total".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_total' AS metric, cast(b_total.value as string) AS value FROM b_total".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'A_minus_B_key' AS metric, cast(ab_key.value as string) AS value FROM ab_key".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_minus_A_key' AS metric, cast(ba_key.value as string) AS value FROM ba_key".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'A_hash_checksum' AS metric, cast(a_hash.value as string) AS value FROM a_hash".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_hash_checksum' AS metric, cast(b_hash.value as string) AS value FROM b_hash".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'A_minus_B_fullrow' AS metric, cast(ab_rows.value as string) AS value FROM ab_rows".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'B_minus_A_fullrow' AS metric, cast(ba_rows.value as string) AS value FROM ba_rows".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'SAMPLE_A_ONLY' AS metric, concat(cast(a_only_sample.__cmp_key as string), '{1}', cast(a_only_sample.__row_text as string)) AS value FROM a_only_sample".format(pair_name, SAMPLE_VALUE_SEPARATOR))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'SAMPLE_B_ONLY' AS metric, concat(cast(b_only_sample.__cmp_key as string), '{1}', cast(b_only_sample.__row_text as string)) AS value FROM b_only_sample".format(pair_name, SAMPLE_VALUE_SEPARATOR))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'FAST_AUDIT_RESULT' AS metric, CASE WHEN a_total.value = b_total.value AND ab_key.value = 0 AND ba_key.value = 0 AND a_hash.value = b_hash.value THEN 'OK' ELSE 'DIFF' END AS value FROM a_total CROSS JOIN b_total CROSS JOIN ab_key CROSS JOIN ba_key CROSS JOIN a_hash CROSS JOIN b_hash".format(pair_name))
    block.append("UNION ALL")
    block.append("SELECT '{0}' AS pair_name, 'STRICT_100_RESULT' AS metric, CASE WHEN ab_rows.value = 0 AND ba_rows.value = 0 THEN 'OK' ELSE 'DIFF' END AS value FROM ab_rows CROSS JOIN ba_rows;".format(pair_name))

    block.append("")
    return "\n".join(block)


def main():
    parser = argparse.ArgumentParser(description="Valida equivalencia entre tablas (modo CSV) o queries (modo SQL).")
    parser.add_argument("--mode", choices=["auto", "pairs", "sql"], default="auto", help="Modo de ejecucion.")
    parser.add_argument("--pairs", default="table_pairs.csv", help="CSV con pares a comparar (modo pairs)")
    parser.add_argument("--output", default="validacion_pares_generada.sql", help="Archivo SQL de salida")
    parser.add_argument("--original-sql", default="", help="Archivo SQL de query original (modo sql)")
    parser.add_argument("--refactor-sql", default="", help="Archivo SQL de query refactor (modo sql)")
    parser.add_argument("--pair-name", default="sql_file_pair", help="Nombre logico del par en modo sql")
    parser.add_argument("--key-columns", default="", help="Columnas clave separadas por ';' (modo sql)")
    parser.add_argument("--temp-db", default="default", help="Base de datos para tablas temporales (modo sql)")
    parser.add_argument("--temp-prefix", default="cmp_tmp", help="Prefijo para tablas temporales (modo sql)")
    parser.add_argument(
        "--auto-columns",
        dest="auto_columns",
        action="store_true",
        help="Activa autodescubrimiento de columnas con DESCRIBE en Impala.",
    )
    parser.add_argument(
        "--no-auto-columns",
        dest="auto_columns",
        action="store_false",
        help="Desactiva autodescubrimiento de columnas con DESCRIBE en Impala.",
    )
    parser.add_argument(
        "--impala-shell",
        default="impala-shell",
        help="Comando de impala-shell (ej: impala-shell o ruta completa).",
    )
    parser.add_argument(
        "--impala-opts",
        default="",
        help="Opciones de conexion para impala-shell (ej: \"-i host:21000 -k\").",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Ejecuta automaticamente el SQL generado usando impala-shell (en modo sql se ejecuta siempre).",
    )
    parser.add_argument(
        "--result-output",
        default="",
        help="Archivo para guardar stdout de la ejecucion (si --run).",
    )
    parser.add_argument(
        "--impala-web-url",
        default="",
        help="URL base del web UI de Impala para metricas (ej: http://host:25000).",
    )
    parser.add_argument(
        "--impala-web-timeout",
        type=int,
        default=10,
        help="Timeout (segundos) para requests al API web de Impala.",
    )
    parser.add_argument(
        "--metrics-json",
        default="",
        help="Archivo JSON para guardar metricas por query.",
    )
    parser.add_argument(
        "--human-report",
        default=DEFAULT_HUMAN_REPORT_PATH,
        help="Archivo TXT de salida para reporte humano lado a lado.",
    )
    parser.set_defaults(auto_columns=True)
    args = parser.parse_args()

    args.impala_shell = resolve_impala_shell_command(args.impala_shell)

    has_original_sql = bool(args.original_sql.strip())
    has_refactor_sql = bool(args.refactor_sql.strip())

    if has_original_sql != has_refactor_sql:
        raise ValueError("Debes indicar ambos archivos: --original-sql y --refactor-sql.")

    if args.mode == "sql":
        if not (has_original_sql and has_refactor_sql):
            raise ValueError("Modo sql requiere --original-sql y --refactor-sql.")
        use_sql_mode = True
    elif args.mode == "pairs":
        if has_original_sql or has_refactor_sql:
            raise ValueError("Modo pairs no admite --original-sql/--refactor-sql.")
        use_sql_mode = False
    else:
        use_sql_mode = has_original_sql and has_refactor_sql

    log_info("Modo de ejecucion seleccionado: {0}".format("sql" if use_sql_mode else "pairs"))

    output_path = args.output
    report_path = (args.human_report or "").strip() or DEFAULT_HUMAN_REPORT_PATH
    temp_tables = []
    metrics_rows = []
    rows = []
    original_query = ""
    refactor_query = ""

    step1_summary = {
        "status": "SKIPPED",
        "all_pass": False,
        "pairs": [],
        "reason": "Step 1 aun no ejecutado",
    }
    step2_summary = {
        "status": "SKIPPED",
        "reason": "Step 2 aun no ejecutado",
        "method": "direct_query",
        "original_metrics": None,
        "refactor_metrics": None,
        "original_rowcount": "",
        "refactor_rowcount": "",
    }
    comparison_samples_by_pair = {}

    impala_web_url = args.impala_web_url.strip() or infer_impala_web_url(args.impala_opts)
    if impala_web_url:
        log_info("metricas API habilitadas via {0}".format(impala_web_url))
    else:
        log_info("metricas API no configuradas; se usara wall-clock y fallback de PROFILE cuando exista.")

    execute_generated_sql = args.run or use_sql_mode
    log_info("Ejecucion de SQL generado: {0}".format("si" if execute_generated_sql else "no"))

    try:
        if use_sql_mode:
            log_info("Iniciando modo SQL con archivos: original={0}, refactor={1}".format(args.original_sql, args.refactor_sql))
            key_columns = split_list(args.key_columns)
            pair_name = args.pair_name.strip() or "sql_file_pair"

            original_table = make_temp_table_name(args.temp_db, args.temp_prefix, pair_name, "original")
            refactor_table = make_temp_table_name(args.temp_db, args.temp_prefix, pair_name, "refactor")
            log_info("Tabla temporal original: {0}".format(original_table))
            log_info("Tabla temporal refactor: {0}".format(refactor_table))

            log_info("Cargando y validando query SQL original")
            original_query = load_sql_query_for_ctas(args.original_sql)
            log_info("Cargando y validando query SQL refactor")
            refactor_query = load_sql_query_for_ctas(args.refactor_sql)

            create_original_sql = "CREATE TABLE {0} AS {1}".format(original_table, original_query)
            create_original_result = run_impala_query_timed(
                create_original_sql,
                args.impala_shell,
                args.impala_opts,
                show_profiles=True,
                step_name="create_original_temp_table",
            )
            metrics_rows.append(
                build_step_metrics(
                    "create_original_temp_table",
                    create_original_result,
                    impala_web_url,
                    args.impala_web_timeout,
                    allow_api=True,
                )
            )
            if create_original_result["returncode"] != 0:
                raise RuntimeError(format_impala_error("CREATE TABLE original", create_original_result))
            temp_tables.append(original_table)

            create_refactor_sql = "CREATE TABLE {0} AS {1}".format(refactor_table, refactor_query)
            create_refactor_result = run_impala_query_timed(
                create_refactor_sql,
                args.impala_shell,
                args.impala_opts,
                show_profiles=True,
                step_name="create_refactor_temp_table",
            )
            metrics_rows.append(
                build_step_metrics(
                    "create_refactor_temp_table",
                    create_refactor_result,
                    impala_web_url,
                    args.impala_web_timeout,
                    allow_api=True,
                )
            )
            if create_refactor_result["returncode"] != 0:
                raise RuntimeError(format_impala_error("CREATE TABLE refactor", create_refactor_result))
            temp_tables.append(refactor_table)

            log_info("Descubriendo columnas via DESCRIBE para tablas temporales")
            cols_a = run_describe(original_table, args.impala_shell, args.impala_opts)
            cols_b = run_describe(refactor_table, args.impala_shell, args.impala_opts)
            common_cols = shared_columns(cols_a, cols_b)
            if not common_cols:
                raise ValueError(
                    "El par {0} no tiene columnas en comun entre {1} y {2}.".format(
                        pair_name, original_table, refactor_table
                    )
                )

            if args.auto_columns and not key_columns:
                key_columns = choose_auto_key_columns(common_cols)

            if not key_columns:
                raise ValueError(
                    "El par {0} no tiene key_columns. Usa --key-columns o --auto-columns.".format(pair_name)
                )

            rows = [(pair_name, original_table, refactor_table, key_columns, common_cols)]
        else:
            log_info("Iniciando modo pairs con CSV: {0}".format(args.pairs))
            rows = build_rows_from_pairs_csv(args)

        sql_parts = [
            "-- SQL generado automaticamente para validacion funcional",
            "-- Metricas: count, anti-join por clave, hash y comparacion estricta por fila completa con multiplicidad.",
            "-- Nota: para hash y validacion estricta se usan las columnas en comun detectadas por DESCRIBE.",
            "",
        ]

        for row_data in rows:
            sql_parts.append(
                metric_block(
                    row_data[0],
                    row_data[1],
                    row_data[2],
                    row_data[3],
                    row_data[4],
                    sample_size=DEFAULT_SAMPLE_SIZE,
                )
            )

        ensure_parent_dir(output_path)
        write_text_file(output_path, "\n".join(sql_parts))
        log_info("SQL generado en {0}".format(output_path))

        if execute_generated_sql:
            log_info("Ejecutando SQL de comparacion en Impala")
            compare_result = run_impala_file_timed(
                output_path,
                args.impala_shell,
                args.impala_opts,
                show_profiles=False,
                step_name="execute_comparison_sql",
                delimited=True,
            )
            metrics_rows.append(
                build_step_metrics(
                    "execute_comparison_sql",
                    compare_result,
                    impala_web_url,
                    args.impala_web_timeout,
                    allow_api=True,
                )
            )

            if args.result_output:
                ensure_parent_dir(args.result_output)
                write_text_file(args.result_output, compare_result["stdout"])

            comparison_metrics_by_pair, comparison_samples_by_pair = parse_comparison_output(compare_result.get("stdout", ""))
            step1_summary = evaluate_step1_results(rows, comparison_metrics_by_pair)

            if compare_result["returncode"] != 0:
                step1_summary["status"] = "ERROR"
                step1_summary["all_pass"] = False
                step1_summary["reason"] = "Error ejecutando SQL de comparacion"
                step2_summary["status"] = "SKIPPED"
                step2_summary["reason"] = "Step 2 omitido por error de ejecucion en Step 1"
                raise RuntimeError(format_impala_error("ejecucion SQL de comparacion", compare_result))

            log_info("SQL ejecutado en Impala")
            if args.result_output:
                log_info("Resultado guardado en {0}".format(args.result_output))

            if step1_summary.get("all_pass"):
                log_info("STEP 1 PASS: STRICT_100_RESULT=OK para todos los pares")
                if use_sql_mode:
                    log_info("STEP 2 START: ejecucion read-only de query original y refactor")

                    original_probe_result = run_impala_query_timed(
                        original_query,
                        args.impala_shell,
                        args.impala_opts,
                        show_profiles=True,
                        step_name="step2_readonly_original_direct",
                        delimited=True,
                    )
                    original_probe_metrics = build_step_metrics(
                        "step2_readonly_original_direct",
                        original_probe_result,
                        impala_web_url,
                        args.impala_web_timeout,
                        allow_api=True,
                    )
                    metrics_rows.append(original_probe_metrics)
                    if original_probe_result["returncode"] != 0:
                        step2_summary["status"] = "ERROR"
                        step2_summary["reason"] = "Fallo la ejecucion read-only de la query original"
                        step2_summary["original_metrics"] = original_probe_metrics
                        raise RuntimeError(format_impala_error("step2_readonly_original", original_probe_result))

                    refactor_probe_result = run_impala_query_timed(
                        refactor_query,
                        args.impala_shell,
                        args.impala_opts,
                        show_profiles=True,
                        step_name="step2_readonly_refactor_direct",
                        delimited=True,
                    )
                    refactor_probe_metrics = build_step_metrics(
                        "step2_readonly_refactor_direct",
                        refactor_probe_result,
                        impala_web_url,
                        args.impala_web_timeout,
                        allow_api=True,
                    )
                    metrics_rows.append(refactor_probe_metrics)
                    if refactor_probe_result["returncode"] != 0:
                        step2_summary["status"] = "ERROR"
                        step2_summary["reason"] = "Fallo la ejecucion read-only de la query refactor"
                        step2_summary["original_metrics"] = original_probe_metrics
                        step2_summary["refactor_metrics"] = refactor_probe_metrics
                        raise RuntimeError(format_impala_error("step2_readonly_refactor", refactor_probe_result))

                    step2_summary = {
                        "status": "COMPLETED",
                        "reason": "",
                        "method": "direct_query",
                        "original_metrics": original_probe_metrics,
                        "refactor_metrics": refactor_probe_metrics,
                        "original_rowcount": "",
                        "refactor_rowcount": "",
                    }
                    log_info("STEP 2 COMPLETED: comparacion read-only finalizada")
                else:
                    step2_summary["status"] = "SKIPPED"
                    step2_summary["reason"] = "Step 2 read-only solo aplica para modo sql"
                    log_warn("STEP 2 SKIPPED: modo pairs")
            else:
                step1_summary["reason"] = "Al menos un par no cumple STRICT_100_RESULT=OK"
                step2_summary["status"] = "SKIPPED"
                step2_summary["reason"] = "Step 1 FAIL: se omite Step 2"
                log_warn("STEP 1 FAIL: se omite Step 2")
        else:
            log_info("SQL no ejecutado (usa --run)")
            step1_summary = {
                "status": "SKIPPED",
                "all_pass": False,
                "pairs": [],
                "reason": "No se ejecuto SQL de comparacion",
            }
            step2_summary = {
                "status": "SKIPPED",
                "reason": "Step 2 requiere que Step 1 se ejecute",
                "method": "direct_query",
                "original_metrics": None,
                "refactor_metrics": None,
                "original_rowcount": "",
                "refactor_rowcount": "",
            }
    finally:
        if use_sql_mode and temp_tables:
            log_info("Iniciando cleanup de tablas temporales")
            cleanup_errors = []
            for temp_table in temp_tables:
                drop_sql = "DROP TABLE IF EXISTS {0}".format(temp_table)
                drop_result = run_impala_query_timed(
                    drop_sql,
                    args.impala_shell,
                    args.impala_opts,
                    show_profiles=False,
                    step_name="drop_temp_table:{0}".format(temp_table),
                )
                metrics_rows.append(
                    build_step_metrics(
                        "drop_temp_table:{0}".format(temp_table),
                        drop_result,
                        impala_web_url,
                        args.impala_web_timeout,
                        allow_api=True,
                    )
                )
                if drop_result["returncode"] != 0:
                    cleanup_errors.append(format_impala_error("DROP TABLE {0}".format(temp_table), drop_result))

            if cleanup_errors:
                log_warn("hubo errores en cleanup de temporales")
                for err in cleanup_errors:
                    write_stdout_line(err)
            else:
                log_info("tablas temporales eliminadas")

        try:
            print_metrics_summary(metrics_rows)
            if args.metrics_json:
                ensure_parent_dir(args.metrics_json)
                write_text_file(args.metrics_json, json.dumps(metrics_rows, indent=2, sort_keys=True))
                log_info("metricas guardadas en {0}".format(args.metrics_json))
        except Exception as exc:
            log_warn("no se pudieron emitir metricas: {0}".format(exc))

        try:
            report_text = build_human_report_text(
                "sql" if use_sql_mode else "pairs",
                step1_summary,
                comparison_samples_by_pair,
                step2_summary,
                DEFAULT_SAMPLE_SIZE,
            )
            ensure_parent_dir(report_path)
            write_text_file(report_path, report_text)
            log_info("reporte humano guardado en {0}".format(report_path))
        except Exception as exc:
            log_warn("no se pudo generar el reporte humano: {0}".format(exc))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        write_stderr_line(exc)
        sys.exit(1)

