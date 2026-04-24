import argparse
import base64
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
import threading
import traceback
import time
from datetime import datetime

try:
    import queue as queue_module
except ImportError:
    queue_module = __import__("Queue")

try:
    from urllib.parse import quote as url_quote
    from urllib.request import Request, urlopen
except ImportError:
    from urllib import quote as url_quote
    from urllib2 import Request, urlopen


QUERY_ID_PATTERN = re.compile(r"([0-9a-fA-F]{16}:[0-9a-fA-F]{16})")
DEFAULT_SAMPLE_SIZE = 50
DEFAULT_STEP2_RUNS = 5
DEFAULT_HUMAN_REPORT_PATH = "comparison_report.txt"
DEFAULT_ELASTIC_INDEX = "impala-metricas-queries"
DEFAULT_ELASTIC_ENV_FILE = ".env"
DEFAULT_ELASTIC_WAIT_SEC = 60
DEFAULT_ELASTIC_TIMEOUT_SEC = 15
SAMPLE_VALUE_SEPARATOR = " ||| "

try:
    text_type = unicode  # type: ignore[name-defined]
except NameError:
    text_type = str


LOG_WRITE_LOCK = threading.Lock()


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
    with LOG_WRITE_LOCK:
        try:
            sys.stderr.write(line)
        except Exception:
            # Python 2 stderr may be bytes-only under some console encodings.
            sys.stderr.write(line.encode("utf-8", "replace"))


def write_stdout_line(message):
    line = to_text(message) + u"\n"
    with LOG_WRITE_LOCK:
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


def load_env_file(path):
    env_map = {}
    if not path:
        return env_map
    if not os.path.exists(path):
        return env_map

    with io.open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = to_text(raw_line).strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]

            env_map[key] = value
    return env_map


def get_env_setting(env_map, key, default=""):
    os_value = os.environ.get(key)
    if os_value not in (None, ""):
        return to_text(os_value).strip()
    env_value = env_map.get(key)
    if env_value not in (None, ""):
        return to_text(env_value).strip()
    return default


def resolve_elastic_config(env_file, index_name):
    env_map = load_env_file(env_file)

    host = get_env_setting(env_map, "ELASTIC_HOST")
    port = get_env_setting(env_map, "ELASTIC_PORT")
    username = get_env_setting(env_map, "ELASTIC_USERNAME")
    password = get_env_setting(env_map, "ELASTIC_PASSWORD")
    scheme = get_env_setting(env_map, "ELASTIC_SCHEME", "http") or "http"
    index = to_text(index_name or "").strip() or DEFAULT_ELASTIC_INDEX

    missing = []
    if not host:
        missing.append("ELASTIC_HOST")
    if not port:
        missing.append("ELASTIC_PORT")
    if not username:
        missing.append("ELASTIC_USERNAME")
    if not password:
        missing.append("ELASTIC_PASSWORD")

    if missing:
        raise ValueError(
            "Faltan variables de Elasticsearch: {0}. Revisa {1}.".format(
                ", ".join(missing),
                env_file or DEFAULT_ELASTIC_ENV_FILE,
            )
        )

    return {
        "scheme": scheme,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "index": index,
    }


def build_basic_auth_header(username, password):
    token = "{0}:{1}".format(to_text(username), to_text(password))
    if sys.version_info[0] >= 3:
        encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
    else:
        encoded = base64.b64encode(token)
    return "Basic " + encoded


def fetch_json_post(url, payload, timeout_sec, auth_header):
    started = time.time()
    body = json.dumps(payload)
    if sys.version_info[0] >= 3:
        body = body.encode("utf-8")

    request = Request(url, data=body)
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    if auth_header:
        request.add_header("Authorization", auth_header)

    response = urlopen(request, timeout=timeout_sec)
    try:
        response_payload = response.read()
    finally:
        if hasattr(response, "close"):
            response.close()

    response_payload = decode_if_bytes(response_payload)
    log_info("END elastic_request: url={0} elapsed={1:.3f}s".format(url, time.time() - started))
    return json.loads(response_payload)


def first_scalar(value):
    if isinstance(value, list):
        for item in value:
            resolved = first_scalar(item)
            if resolved not in (None, ""):
                return resolved
        return None
    return value


def extract_query_id_from_elastic_doc(doc):
    for key in ("query_id", "queryId", "queryid", "id", "_id"):
        if key in doc:
            qid = first_scalar(doc.get(key))
            if qid:
                return normalize_query_id(qid)
    return ""


def sum_prefixed_numeric_fields(doc, field_prefix):
    total = 0.0
    has_any_value = False

    for key, raw_value in (doc or {}).items():
        if not to_text(key).startswith(field_prefix):
            continue

        series = parse_numeric_series(raw_value)
        if not series:
            continue

        has_any_value = True
        total += sum(series)

    if not has_any_value:
        return None
    return total


def extract_elastic_query_metrics(doc):
    metrics = {
        "duration_ms": None,
        "cpu_total": None,
        "memory_total_bytes": None,
        "warnings": [],
    }

    duration_raw = first_scalar(pick_first(doc, ["duration_ms", "duration"]))
    duration_ms = parse_duration_to_ms(duration_raw)
    if duration_ms is not None:
        metrics["duration_ms"] = duration_ms
    else:
        metrics["warnings"].append("elastic_duration_missing")

    cpu_total = sum_prefixed_numeric_fields(doc, "cpu_time_per_host.")
    if cpu_total is None:
        metrics["warnings"].append("elastic_cpu_missing")
    else:
        metrics["cpu_total"] = cpu_total

    memory_total = sum_prefixed_numeric_fields(doc, "mem_per_host.")
    if memory_total is None:
        cluster_memory_series = parse_numeric_series(pick_first(doc, ["cluster_memory_admitted_bytes"]))
        if cluster_memory_series:
            memory_total = sum(cluster_memory_series)
    if memory_total is None:
        metrics["warnings"].append("elastic_mem_missing")
    else:
        metrics["memory_total_bytes"] = memory_total

    return metrics


def build_elastic_search_payload(query_ids):
    normalized_ids = [normalize_query_id(qid) for qid in query_ids if normalize_query_id(qid)]
    return {
        "size": max(10, len(normalized_ids) * 3),
        "query": {
            "bool": {
                "should": [
                    {"terms": {"query_id.keyword": normalized_ids}},
                    {"terms": {"query_id": normalized_ids}},
                    {"ids": {"values": normalized_ids}},
                ],
                "minimum_should_match": 1,
            }
        },
    }


def fetch_elastic_docs_by_query_ids(query_ids, elastic_config, timeout_sec):
    base_url = "{0}://{1}:{2}".format(
        elastic_config["scheme"],
        elastic_config["host"],
        elastic_config["port"],
    )
    index_name = elastic_config["index"]
    payload = build_elastic_search_payload(query_ids)
    auth_header = build_basic_auth_header(elastic_config["username"], elastic_config["password"])

    endpoints = [
        "{0}/{1}/_search".format(base_url, url_quote(index_name)),
        "{0}/{1}-*/_search".format(base_url, url_quote(index_name)),
    ]

    last_error = None
    for endpoint in endpoints:
        try:
            log_info("START elastic_request: url={0} timeout={1}s".format(endpoint, timeout_sec))
            payload_json = fetch_json_post(endpoint, payload, timeout_sec, auth_header)
            hits = (((payload_json or {}).get("hits") or {}).get("hits") or [])
            docs_by_query_id = {}
            for hit in hits:
                source = hit.get("_source") if isinstance(hit, dict) else None
                if not isinstance(source, dict):
                    continue
                doc = dict(source)
                if "_id" not in doc and isinstance(hit, dict) and hit.get("_id"):
                    doc["_id"] = hit.get("_id")

                query_id = extract_query_id_from_elastic_doc(doc)
                if query_id and query_id not in docs_by_query_id:
                    docs_by_query_id[query_id] = doc
            return docs_by_query_id
        except Exception as exc:
            last_error = exc

    raise RuntimeError(
        "No se pudo consultar Elasticsearch en los endpoints esperados del indice {0}: {1}".format(
            index_name,
            last_error,
        )
    )


def wall_clock_ms_from_metric(metric_row):
    elapsed = to_float_metric((metric_row or {}).get("elapsed_wall_sec"))
    if elapsed is None:
        return None
    return elapsed * 1000.0


def append_metric_warning(metric_row, warning):
    warning_text = to_text(warning).strip()
    if not warning_text:
        return
    warning_list = metric_row.setdefault("metric_warnings", [])
    if warning_text not in warning_list:
        warning_list.append(warning_text)


def enrich_step2_metrics_from_elastic(step2_run_metrics, elastic_config, wait_sec, timeout_sec):
    if not step2_run_metrics:
        return

    lookup_ids = []
    seen_ids = set()
    for run_metric in step2_run_metrics:
        query_id = normalize_query_id(run_metric.get("query_id"))
        if query_id and query_id not in seen_ids:
            lookup_ids.append(query_id)
            seen_ids.add(query_id)

    if not lookup_ids:
        for run_metric in step2_run_metrics:
            run_metric["duration_ms"] = wall_clock_ms_from_metric(run_metric)
            run_metric["api_duration_ms"] = run_metric["duration_ms"]
            run_metric["cpu_total"] = None
            run_metric["memory_total_bytes"] = None
            run_metric["peak_memory_bytes"] = None
            run_metric["metric_source"] = "wall_clock_backup"
            append_metric_warning(run_metric, "query_id_missing_for_elastic_lookup")
        return

    wait_seconds = max(0, int(wait_sec or 0))
    if wait_seconds > 0:
        log_info(
            "Esperando {0}s antes de consultar Elasticsearch para {1} query_ids de Step 2".format(
                wait_seconds,
                len(lookup_ids),
            )
        )
        time.sleep(wait_seconds)

    docs_by_query_id = fetch_elastic_docs_by_query_ids(lookup_ids, elastic_config, timeout_sec)

    for run_metric in step2_run_metrics:
        query_id = normalize_query_id(run_metric.get("query_id"))
        fallback_duration_ms = wall_clock_ms_from_metric(run_metric)

        run_metric["duration_ms"] = fallback_duration_ms
        run_metric["api_duration_ms"] = fallback_duration_ms
        run_metric["cpu_total"] = None
        run_metric["memory_total_bytes"] = None
        run_metric["peak_memory_bytes"] = None

        if not query_id:
            run_metric["metric_source"] = "wall_clock_backup"
            append_metric_warning(run_metric, "query_id_missing_for_elastic_lookup")
            continue

        doc = docs_by_query_id.get(query_id)
        if doc is None:
            run_metric["metric_source"] = "wall_clock_backup"
            append_metric_warning(run_metric, "elastic_doc_missing:{0}".format(query_id))
            continue

        doc_metrics = extract_elastic_query_metrics(doc)
        source_tags = ["elastic_index"]

        if doc_metrics["duration_ms"] is not None:
            run_metric["duration_ms"] = doc_metrics["duration_ms"]
            run_metric["api_duration_ms"] = doc_metrics["duration_ms"]
        else:
            source_tags.append("wall_clock_backup")

        run_metric["cpu_total"] = doc_metrics["cpu_total"]
        run_metric["memory_total_bytes"] = doc_metrics["memory_total_bytes"]
        run_metric["peak_memory_bytes"] = doc_metrics["memory_total_bytes"]

        for warning in doc_metrics.get("warnings") or []:
            append_metric_warning(run_metric, "{0}:{1}".format(warning, query_id))

        run_metric["metric_source"] = ",".join(source_tags)


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


def build_step_metrics(step_name, result, impala_web_url=None, web_timeout_sec=None, allow_api=False):
    # Mantiene firma compatible con llamadas existentes; ahora el enriquecimiento viene por Elasticsearch.
    _ = impala_web_url
    _ = web_timeout_sec
    _ = allow_api

    combined_output = (result.get("stdout") or "") + "\n" + (result.get("stderr") or "")
    query_id = extract_query_id(combined_output)
    elapsed_sec = to_float_metric(result.get("elapsed_sec"))

    metrics = {
        "step": step_name,
        "status": "OK" if result.get("returncode") == 0 else "ERROR",
        "return_code": result.get("returncode"),
        "query_id": query_id,
        "started_at_utc": result.get("started_at_utc"),
        "ended_at_utc": result.get("ended_at_utc"),
        "elapsed_wall_sec": elapsed_sec,
        "duration_ms": elapsed_sec * 1000.0 if elapsed_sec is not None else None,
        "api_duration_ms": None,
        "peak_memory_bytes": None,
        "memory_total_bytes": None,
        "cpu_total": None,
        "metric_source": "wall_clock_only",
        "metric_warnings": [],
    }
    metrics["api_duration_ms"] = metrics["duration_ms"]
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
            "  source={0} duration_ms={1} mem_total_mb={2} cpu_total={3}".format(
                row["metric_source"],
                "{0:.2f}".format(to_float_metric(row.get("duration_ms")))
                if to_float_metric(row.get("duration_ms")) is not None
                else "n/a",
                format_mb(row.get("memory_total_bytes") if row.get("memory_total_bytes") is not None else row.get("peak_memory_bytes")),
                "{0:.3f}".format(to_float_metric(row.get("cpu_total"))) if to_float_metric(row.get("cpu_total")) is not None else "n/a",
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


def median_value(values):
    if not values:
        return None

    ordered = sorted(values)
    size = len(ordered)
    mid = size // 2
    if size % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def metric_values_from_runs(run_metrics, metric_key):
    values = []
    for run_metric in run_metrics or []:
        parsed = to_float_metric(run_metric.get(metric_key))
        if parsed is not None:
            values.append(parsed)
    return values


def run_index_value(run_metric):
    try:
        return int(run_metric.get("run_index", 0))
    except Exception:
        return 0


def format_run_metric_series(run_metrics, metric_key, formatter):
    if not run_metrics:
        return "n/a"

    parts = []
    ordered_runs = sorted(run_metrics, key=run_index_value)
    for run_metric in ordered_runs:
        run_number = run_metric.get("run_index", "?")
        parsed = to_float_metric(run_metric.get(metric_key))
        formatted = formatter(parsed) if parsed is not None else "n/a"
        parts.append("r{0}={1}".format(run_number, formatted))
    return ", ".join(parts)


def aggregate_step2_side_metrics(run_metrics):
    aggregated = {
        "duration_ms": None,
        "elapsed_wall_sec": None,
        "peak_memory_bytes": None,
        "memory_total_bytes": None,
        "cpu_total": None,
        "duration_ms_sample_count": 0,
        "elapsed_wall_sec_sample_count": 0,
        "peak_memory_bytes_sample_count": 0,
        "memory_total_bytes_sample_count": 0,
        "cpu_total_sample_count": 0,
        "metric_source": "",
        "metric_warnings": [],
        "run_count": len(run_metrics or []),
    }

    metric_keys = (
        "duration_ms",
        "elapsed_wall_sec",
        "peak_memory_bytes",
        "memory_total_bytes",
        "cpu_total",
    )
    for metric_key in metric_keys:
        values = metric_values_from_runs(run_metrics, metric_key)
        aggregated[metric_key] = median_value(values)
        aggregated[metric_key + "_sample_count"] = len(values)

    source_seen = []
    for run_metric in run_metrics or []:
        source = to_text(run_metric.get("metric_source", "")).strip()
        if source and source not in source_seen:
            source_seen.append(source)
        for warning in run_metric.get("metric_warnings") or []:
            warning_text = to_text(warning)
            if warning_text and warning_text not in aggregated["metric_warnings"]:
                aggregated["metric_warnings"].append(warning_text)

    aggregated["metric_source"] = ",".join(source_seen) if source_seen else "wall_clock_only"
    return aggregated


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

    lines.append("Metodo: ejecucion directa de cada query en modo read-only + lookup diferido en Elasticsearch.")
    lines.append(
        "Politica: {0} | runs por query: {1}".format(
            step2_summary.get("policy", "n/a"),
            step2_summary.get("runs", "n/a"),
        )
    )
    if step2_summary.get("original_rowcount") or step2_summary.get("refactor_rowcount"):
        lines.append("Rowcount original: {0}".format(step2_summary.get("original_rowcount") or "n/a"))
        lines.append("Rowcount refactor: {0}".format(step2_summary.get("refactor_rowcount") or "n/a"))

    original_metrics = step2_summary.get("original_metrics") or {}
    refactor_metrics = step2_summary.get("refactor_metrics") or {}
    original_runs = step2_summary.get("original_runs") or []
    refactor_runs = step2_summary.get("refactor_runs") or []

    lines.append("Fuente metricas original: {0}".format(original_metrics.get("metric_source", "n/a")))
    lines.append("Fuente metricas refactor: {0}".format(refactor_metrics.get("metric_source", "n/a")))
    lines.append("")

    lines.append(
        "Series tiempo original: {0}".format(
            format_run_metric_series(
                original_runs,
                "duration_ms",
                lambda v: "n/a" if v is None else "{0:.2f}ms".format(v),
            )
        )
    )
    lines.append(
        "Series tiempo refactor: {0}".format(
            format_run_metric_series(
                refactor_runs,
                "duration_ms",
                lambda v: "n/a" if v is None else "{0:.2f}ms".format(v),
            )
        )
    )
    lines.append("")
    lines.append("Comparacion por mediana:")

    lines.append(
        format_efficiency_line(
            "Tiempo query duration_ms [n_orig={0}, n_ref={1}]".format(
                original_metrics.get("duration_ms_sample_count", 0),
                refactor_metrics.get("duration_ms_sample_count", 0),
            ),
            original_metrics.get("duration_ms"),
            refactor_metrics.get("duration_ms"),
            lambda v: "n/a" if v is None else "{0:.2f}ms".format(v),
            lower_is_better=True,
        )
    )
    lines.append(
        format_efficiency_line(
            "Memoria total host-sum [n_orig={0}, n_ref={1}]".format(
                original_metrics.get("memory_total_bytes_sample_count", 0),
                refactor_metrics.get("memory_total_bytes_sample_count", 0),
            ),
            original_metrics.get("memory_total_bytes"),
            refactor_metrics.get("memory_total_bytes"),
            lambda v: "n/a" if v is None else "{0:.2f}MB".format(v / (1024.0 * 1024.0)),
            lower_is_better=True,
        )
    )
    lines.append(
        format_efficiency_line(
            "CPU total host-sum [n_orig={0}, n_ref={1}]".format(
                original_metrics.get("cpu_total_sample_count", 0),
                refactor_metrics.get("cpu_total_sample_count", 0),
            ),
            original_metrics.get("cpu_total"),
            refactor_metrics.get("cpu_total"),
            lambda v: "n/a" if v is None else "{0:.3f}".format(v),
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


def build_step2_error_summary(run_total, reason, original_run_metrics, refactor_run_metrics):
    return {
        "status": "ERROR",
        "reason": reason,
        "method": "direct_query_elastic_lookup",
        "policy": "alternating",
        "runs": run_total,
        "original_metrics": aggregate_step2_side_metrics(original_run_metrics),
        "refactor_metrics": aggregate_step2_side_metrics(refactor_run_metrics),
        "original_runs": original_run_metrics,
        "refactor_runs": refactor_run_metrics,
        "original_rowcount": "",
        "refactor_rowcount": "",
    }


def execute_step2_query_worker(result_queue, side, query_text, run_number, round_order, args, impala_web_url):
    step_name = "step2_readonly_{0}_run{1}".format(side, run_number)
    try:
        run_result = run_impala_query_timed(
            query_text,
            args.impala_shell,
            args.impala_opts,
            show_profiles=True,
            step_name=step_name,
            delimited=True,
        )
        run_metric = build_step_metrics(
            step_name,
            run_result,
            impala_web_url,
            args.elastic_timeout,
            allow_api=False,
        )
        run_metric["query_side"] = side
        run_metric["run_index"] = run_number
        run_metric["round_order"] = round_order

        result_queue.put(
            {
                "ok": True,
                "side": side,
                "run_index": run_number,
                "round_order": round_order,
                "step_name": step_name,
                "run_result": run_result,
                "run_metric": run_metric,
            }
        )
    except Exception as exc:
        result_queue.put(
            {
                "ok": False,
                "side": side,
                "run_index": run_number,
                "round_order": round_order,
                "step_name": step_name,
                "error": to_text(exc),
                "traceback": traceback.format_exc(),
            }
        )


def run_step2_sql_mode(original_query, refactor_query, args, metrics_rows, impala_web_url):
    run_total = int(args.step2_runs)
    elastic_config = resolve_elastic_config(args.elastic_env_file, args.elastic_index)
    log_info(
        "STEP 2 START: ejecucion read-only directa, {0} runs por query, pares concurrentes por run; metricas via Elasticsearch.".format(
            run_total
        )
    )

    original_run_metrics = []
    refactor_run_metrics = []
    step2_run_metrics = []

    for run_idx in range(run_total):
        run_number = run_idx + 1
        if run_idx % 2 == 0:
            run_plan = [("original", original_query), ("refactor", refactor_query)]
        else:
            run_plan = [("refactor", refactor_query), ("original", original_query)]

        log_info(
            "STEP 2 ROUND {0}/{1}: lanzando en paralelo ({2},{3})".format(
                run_number,
                run_total,
                run_plan[0][0],
                run_plan[1][0],
            )
        )

        round_queue = queue_module.Queue()
        round_threads = []
        for round_order, plan_item in enumerate(run_plan, 1):
            side = plan_item[0]
            query_text = plan_item[1]
            worker = threading.Thread(
                target=execute_step2_query_worker,
                args=(round_queue, side, query_text, run_number, round_order, args, impala_web_url),
            )
            worker.daemon = False
            worker.start()
            round_threads.append(worker)

        for worker in round_threads:
            worker.join()

        round_results = []
        while len(round_results) < len(run_plan):
            round_results.append(round_queue.get())

        round_results.sort(key=lambda item: item.get("round_order", 0))

        for item in round_results:
            side = item.get("side", "")
            if not item.get("ok"):
                reason = "Error interno Step 2 en run {0} lado {1}".format(run_number, side)
                step2_summary = build_step2_error_summary(
                    run_total,
                    reason,
                    original_run_metrics,
                    refactor_run_metrics,
                )
                error_text = item.get("error") or "error desconocido"
                trace_text = item.get("traceback") or ""
                return step2_summary, "{0}. Detalle: {1}\n{2}".format(reason, error_text, trace_text)

            run_result = item.get("run_result") or {}
            run_metric = item.get("run_metric") or {}

            metrics_rows.append(run_metric)
            step2_run_metrics.append(run_metric)

            if side == "original":
                original_run_metrics.append(run_metric)
            else:
                refactor_run_metrics.append(run_metric)

            if run_result.get("returncode") != 0:
                reason = "Fallo Step 2 en run {0} lado {1}".format(run_number, side)
                step2_summary = build_step2_error_summary(
                    run_total,
                    reason,
                    original_run_metrics,
                    refactor_run_metrics,
                )
                return step2_summary, format_impala_error(item.get("step_name") or "step2", run_result)

    try:
        enrich_step2_metrics_from_elastic(
            step2_run_metrics,
            elastic_config,
            args.elastic_wait_sec,
            args.elastic_timeout,
        )
        log_info("STEP 2 METRICS: enriquecimiento Elasticsearch completado")
    except Exception as exc:
        log_warn("STEP 2 METRICS: lookup Elasticsearch fallo, se usa fallback wall-clock ({0})".format(exc))
        for run_metric in step2_run_metrics:
            run_metric["duration_ms"] = wall_clock_ms_from_metric(run_metric)
            run_metric["api_duration_ms"] = run_metric["duration_ms"]
            run_metric["cpu_total"] = None
            run_metric["memory_total_bytes"] = None
            run_metric["peak_memory_bytes"] = None
            run_metric["metric_source"] = "wall_clock_backup"
            append_metric_warning(run_metric, "elastic_lookup_error:{0}".format(exc))

    step2_summary = {
        "status": "COMPLETED",
        "reason": "",
        "method": "direct_query_elastic_lookup",
        "policy": "alternating",
        "runs": run_total,
        "original_metrics": aggregate_step2_side_metrics(original_run_metrics),
        "refactor_metrics": aggregate_step2_side_metrics(refactor_run_metrics),
        "original_runs": original_run_metrics,
        "refactor_runs": refactor_run_metrics,
        "original_rowcount": "",
        "refactor_rowcount": "",
    }
    log_info("STEP 2 COMPLETED: comparacion read-only multi-run finalizada")
    return step2_summary, ""


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
        "--elastic-env-file",
        default=DEFAULT_ELASTIC_ENV_FILE,
        help="Ruta al archivo .env con credenciales de Elasticsearch.",
    )
    parser.add_argument(
        "--elastic-index",
        default=DEFAULT_ELASTIC_INDEX,
        help="Indice base de Elasticsearch para metricas de queries.",
    )
    parser.add_argument(
        "--elastic-wait-sec",
        type=int,
        default=DEFAULT_ELASTIC_WAIT_SEC,
        help="Segundos de espera antes del lookup en Elasticsearch luego de la ultima query de Step 2.",
    )
    parser.add_argument(
        "--elastic-timeout",
        type=int,
        default=DEFAULT_ELASTIC_TIMEOUT_SEC,
        help="Timeout (segundos) para requests HTTP a Elasticsearch.",
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
    parser.add_argument(
        "--step2-runs",
        type=int,
        default=DEFAULT_STEP2_RUNS,
        help="Cantidad de ejecuciones por query en Step 2 (alternadas para reducir sesgo).",
    )
    parser.add_argument(
        "--skip-step1",
        action="store_true",
        help="Omite Step 1 y ejecuta directamente Step 2 (solo modo sql).",
    )
    parser.set_defaults(auto_columns=True)
    args = parser.parse_args()

    if args.step2_runs <= 0:
        raise ValueError("--step2-runs debe ser mayor que 0.")
    if args.elastic_wait_sec < 0:
        raise ValueError("--elastic-wait-sec no puede ser negativo.")
    if args.elastic_timeout <= 0:
        raise ValueError("--elastic-timeout debe ser mayor que 0.")

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

    if args.skip_step1 and not use_sql_mode:
        raise ValueError("--skip-step1 solo se permite en modo sql con --original-sql y --refactor-sql.")

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
        "method": "direct_query_elastic_lookup",
        "policy": "alternating",
        "runs": args.step2_runs,
        "original_metrics": None,
        "refactor_metrics": None,
        "original_runs": [],
        "refactor_runs": [],
        "original_rowcount": "",
        "refactor_rowcount": "",
    }
    comparison_samples_by_pair = {}

    impala_web_url = ""
    log_info(
        "Step 2 usara metricas de Elasticsearch (indice={0}, env_file={1}, wait={2}s).".format(
            args.elastic_index,
            args.elastic_env_file,
            args.elastic_wait_sec,
        )
    )

    execute_generated_sql = (args.run or use_sql_mode) and not args.skip_step1
    if args.skip_step1:
        log_info("STEP 1 sera omitido por parametro --skip-step1")
    log_info("Ejecucion de SQL generado: {0}".format("si" if execute_generated_sql else "no"))

    try:
        if use_sql_mode:
            log_info("Iniciando modo SQL con archivos: original={0}, refactor={1}".format(args.original_sql, args.refactor_sql))
            key_columns = split_list(args.key_columns)
            pair_name = args.pair_name.strip() or "sql_file_pair"

            log_info("Cargando y validando query SQL original")
            original_query = load_sql_query_for_ctas(args.original_sql)
            log_info("Cargando y validando query SQL refactor")
            refactor_query = load_sql_query_for_ctas(args.refactor_sql)

            if not args.skip_step1:
                original_table = make_temp_table_name(args.temp_db, args.temp_prefix, pair_name, "original")
                refactor_table = make_temp_table_name(args.temp_db, args.temp_prefix, pair_name, "refactor")
                log_info("Tabla temporal original: {0}".format(original_table))
                log_info("Tabla temporal refactor: {0}".format(refactor_table))

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
                        args.elastic_timeout,
                        allow_api=False,
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
                        args.elastic_timeout,
                        allow_api=False,
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

        if not args.skip_step1:
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
                    args.elastic_timeout,
                    allow_api=False,
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
                    step2_summary, step2_error = run_step2_sql_mode(
                        original_query,
                        refactor_query,
                        args,
                        metrics_rows,
                        impala_web_url,
                    )
                    if step2_error:
                        raise RuntimeError(step2_error)
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
            if args.skip_step1 and use_sql_mode:
                log_info("STEP 1 SKIPPED: se ejecuta Step 2 directamente")
                step1_summary = {
                    "status": "SKIPPED",
                    "all_pass": False,
                    "pairs": [],
                    "reason": "Step 1 omitido por --skip-step1",
                }
                step2_summary, step2_error = run_step2_sql_mode(
                    original_query,
                    refactor_query,
                    args,
                    metrics_rows,
                    impala_web_url,
                )
                if step2_error:
                    raise RuntimeError(step2_error)
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
                    "method": "direct_query_elastic_lookup",
                    "policy": "alternating",
                    "runs": args.step2_runs,
                    "original_metrics": None,
                    "refactor_metrics": None,
                    "original_runs": [],
                    "refactor_runs": [],
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
                        args.elastic_timeout,
                        allow_api=False,
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

