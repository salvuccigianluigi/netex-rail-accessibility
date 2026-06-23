from pathlib import Path
from collections import deque, defaultdict
from bisect import bisect_left
import threading
import queue
import re
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import pandas as pd


# =========================================================
# PERCORSI PREIMPOSTATI
# =========================================================

DEFAULT_JOURNEY_PASSAGES_CSV = r"N:\NETEX\output_netex_elab\journey_passages.csv"
DEFAULT_BASE_NETEX_DIR = r"N:\NETEX\output_netex"
DEFAULT_OUTPUT_DIR = r"N:\NETEX\output_raggiungibili_0700_0800_max2cambi"


# =========================================================
# UTILS TEMPO / STRINGHE
# =========================================================

def parse_time_with_offset(time_str, day_offset=0):
    """
    Converte HH:MM:SS + day_offset in minuti assoluti.
    Esempio:
      07:00:00 + 0 -> 420
      00:20:00 + 1 -> 1460
    """
    if pd.isna(time_str):
        return None

    try:
        h, m, s = map(int, str(time_str).strip().split(":"))
    except Exception:
        return None

    offset = 0
    if not pd.isna(day_offset):
        try:
            offset = int(day_offset)
        except Exception:
            offset = 0

    return offset * 1440 + h * 60 + m


def parse_time_hhmmss(time_str):
    return parse_time_with_offset(time_str, 0)


def minutes_to_hhmm(total_minutes):
    if total_minutes is None or pd.isna(total_minutes):
        return ""

    total_minutes = int(total_minutes)
    d = total_minutes // 1440
    m = total_minutes % 1440
    hh = m // 60
    mm = m % 60

    if d == 0:
        return f"{hh:02d}:{mm:02d}"
    return f"+{d} {hh:02d}:{mm:02d}"


def normalizza_nome(x):
    if pd.isna(x):
        return ""
    return str(x).upper().strip()


def sanitize_filename(text):
    if pd.isna(text) or text is None:
        return "SENZA_NOME"
    text = str(text).strip()
    text = text.replace(" ", "_")
    text = re.sub(r'[\\/*?:"<>|]+', "_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("_")
    return text if text else "SENZA_NOME"


def sanitize_id(text):
    if pd.isna(text) or text is None:
        return "ID_SCONOSCIUTO"
    text = str(text).strip()
    text = re.sub(r'[\\/*?:"<>|]+', "_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("_")
    return text if text else "ID_SCONOSCIUTO"


def make_output_filename(stop_id, station_name):
    return f"{sanitize_id(stop_id)}_{sanitize_filename(station_name)}_best.csv"


def flag_netex_true_or_empty(x):
    """
    Interpreta i flag NeTEx.
    Se il campo è vuoto, lo consideriamo ammesso.
    Escludiamo solo valori esplicitamente negativi.
    """
    if pd.isna(x) or str(x).strip() == "":
        return True

    v = str(x).strip().lower()

    if v in {"false", "0", "no", "n"}:
        return False

    return True


# =========================================================
# FILTRO CALENDARIO
# =========================================================

def carica_day_types_validi(base_netex_dir, data_viaggio, log=None):
    """
    Restituisce l'insieme dei day_type_ref validi nella data indicata.

    Usa:
      - operating_periods.csv
      - day_type_assignments.csv
    """
    base = Path(base_netex_dir)

    op_path = base / "operating_periods.csv"
    dta_path = base / "day_type_assignments.csv"

    if not op_path.exists():
        raise FileNotFoundError(f"Non trovo operating_periods.csv in: {base}")

    if not dta_path.exists():
        raise FileNotFoundError(f"Non trovo day_type_assignments.csv in: {base}")

    target = pd.to_datetime(data_viaggio).date()

    op = pd.read_csv(op_path, dtype=str)
    dta = pd.read_csv(dta_path, dtype=str)

    valid_day_types = set()

    # -----------------------------------------------------
    # 1. Date esplicite in day_type_assignments.csv
    # -----------------------------------------------------
    if "date" in dta.columns:
        dta_dates = dta[dta["date"].notna()].copy()
        if not dta_dates.empty:
            dta_dates["date_parsed"] = pd.to_datetime(
                dta_dates["date"],
                errors="coerce"
            ).dt.date

            valid_day_types.update(
                dta_dates.loc[
                    dta_dates["date_parsed"] == target,
                    "day_type_ref"
                ].dropna().tolist()
            )

    # -----------------------------------------------------
    # 2. Periodi operativi con valid_day_bits
    # -----------------------------------------------------
    required_op = {"operating_period_id", "from_date", "to_date", "valid_day_bits"}
    missing_op = required_op - set(op.columns)
    if missing_op:
        raise ValueError(f"In operating_periods.csv mancano colonne: {missing_op}")

    required_dta = {"day_type_ref", "operating_period_ref"}
    missing_dta = required_dta - set(dta.columns)
    if missing_dta:
        raise ValueError(f"In day_type_assignments.csv mancano colonne: {missing_dta}")

    op = op.copy()
    op["from_date_parsed"] = pd.to_datetime(op["from_date"], errors="coerce").dt.date
    op["to_date_parsed"] = pd.to_datetime(op["to_date"], errors="coerce").dt.date

    dta_period = dta[dta["operating_period_ref"].notna()].copy()

    joined = dta_period.merge(
        op,
        left_on="operating_period_ref",
        right_on="operating_period_id",
        how="left"
    )

    for r in joined.itertuples(index=False):
        day_type_ref = getattr(r, "day_type_ref", None)
        from_date = getattr(r, "from_date_parsed", None)
        to_date = getattr(r, "to_date_parsed", None)
        bits = getattr(r, "valid_day_bits", None)

        if pd.isna(day_type_ref) or pd.isna(from_date) or pd.isna(to_date) or pd.isna(bits):
            continue

        if not (from_date <= target <= to_date):
            continue

        idx = (target - from_date).days
        bits = str(bits).strip()

        if 0 <= idx < len(bits):
            if bits[idx] == "1":
                valid_day_types.add(day_type_ref)

    if log:
        log(f"DayType validi per {data_viaggio}: {len(valid_day_types):,}")

    return valid_day_types


def filtra_journey_passages_per_data(df, base_netex_dir, data_viaggio, log=None):
    """
    Filtra journey_passages.csv tenendo solo le corse valide nella data scelta.
    """
    if not data_viaggio or str(data_viaggio).strip() == "":
        if log:
            log("Nessuna data viaggio indicata: filtro calendario non applicato.")
        return df

    if "day_type_ref" not in df.columns:
        raise ValueError(
            "Nel journey_passages.csv manca day_type_ref: "
            "non posso filtrare per giorno di validità."
        )

    valid_day_types = carica_day_types_validi(
        base_netex_dir=base_netex_dir,
        data_viaggio=data_viaggio,
        log=log
    )

    if not valid_day_types:
        raise ValueError(
            f"Nessun day_type valido trovato per la data {data_viaggio}. "
            "Verificare operating_periods.csv e day_type_assignments.csv."
        )

    before = len(df)

    df = df[df["day_type_ref"].isin(valid_day_types)].copy()

    after = len(df)

    if log:
        log(f"Filtro calendario {data_viaggio}: righe da {before:,} a {after:,}")

    return df


# =========================================================
# CARICAMENTO DATI
# =========================================================

def carica_journey_passages(path_csv, log=None):
    if log:
        log(f"Carico: {path_csv}")

    df = pd.read_csv(path_csv, dtype=str)

    required = {
        "service_journey_id",
        "sequence",
        "stop_place_ref",
        "station_name",
        "departure_time",
        "departure_day_offset",
        "arrival_time",
        "arrival_day_offset",
    }

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Mancano colonne obbligatorie in journey_passages.csv: {missing}")

    df["sequence"] = pd.to_numeric(df["sequence"], errors="coerce")

    df["dep_minutes"] = df.apply(
        lambda r: parse_time_with_offset(
            r.get("departure_time"),
            r.get("departure_day_offset")
        ),
        axis=1
    )

    df["arr_minutes"] = df.apply(
        lambda r: parse_time_with_offset(
            r.get("arrival_time"),
            r.get("arrival_day_offset")
        ),
        axis=1
    )

    # Se manca partenza uso arrivo; se manca arrivo uso partenza.
    df["event_dep"] = df["dep_minutes"].where(df["dep_minutes"].notna(), df["arr_minutes"])
    df["event_arr"] = df["arr_minutes"].where(df["arr_minutes"].notna(), df["dep_minutes"])

    df = df[df["stop_place_ref"].notna()].copy()
    df = df[df["service_journey_id"].notna()].copy()
    df = df[df["sequence"].notna()].copy()

    if log:
        log(f"Righe valide caricate: {len(df):,}")

    return df


def costruisci_indici(df, log=None):
    """
    Costruisce:
      - trips: service_journey_id -> lista fermate ordinate
      - departures_by_stop: stop_id -> lista imbarchi ordinati per partenza
      - dep_times_by_stop: stop_id -> solo lista tempi, per bisect
      - station_registry: anagrafica stazioni
    """

    if log:
        log("Costruisco indici in memoria...")

    trips = {}
    departures_by_stop = defaultdict(list)

    df = df.sort_values(["service_journey_id", "sequence"], kind="stable").copy()

    for sj_id, g in df.groupby("service_journey_id", sort=False):
        g = g.sort_values("sequence", kind="stable").copy()
        rows = g.to_dict("records")
        trips[sj_id] = rows

        for pos, r in enumerate(rows):
            stop_id = r.get("stop_place_ref")
            dep = r.get("event_dep")

            if pd.isna(stop_id) or pd.isna(dep):
                continue

            departures_by_stop[stop_id].append((float(dep), sj_id, pos))

    dep_times_by_stop = {}

    for stop_id in departures_by_stop:
        departures_by_stop[stop_id].sort(key=lambda x: x[0])
        dep_times_by_stop[stop_id] = [x[0] for x in departures_by_stop[stop_id]]

    extra_cols = []
    for c in ["PRO_COM", "COMUNE", "SIGLA_PROV", "COD_REG"]:
        if c in df.columns:
            extra_cols.append(c)

    station_registry = (
        df[["stop_place_ref", "station_name"] + extra_cols]
        .dropna(subset=["stop_place_ref"])
        .drop_duplicates()
        .rename(columns={"stop_place_ref": "stop_id"})
        .sort_values(["station_name", "stop_id"], kind="stable")
        .reset_index(drop=True)
    )

    station_registry = (
        station_registry
        .drop_duplicates(subset=["stop_id"], keep="first")
        .reset_index(drop=True)
    )

    if log:
        log(f"Corse indicizzate: {len(trips):,}")
        log(f"Stazioni indicizzate: {len(station_registry):,}")

    return trips, departures_by_stop, dep_times_by_stop, station_registry


# =========================================================
# COSTRUZIONE RECORD OUTPUT
# =========================================================

def leg_to_record(leg, prefix):
    return {
        f"{prefix}_service_journey_id": leg.get("service_journey_id"),
        f"{prefix}_journey_name": leg.get("journey_name"),
        f"{prefix}_line_name": leg.get("line_name"),
        f"{prefix}_day_type_ref": leg.get("day_type_ref"),

        f"{prefix}_origin_stop_id": leg.get("origin_stop_id"),
        f"{prefix}_origin_station": leg.get("origin_station"),
        f"{prefix}_origin_for_boarding": leg.get("origin_for_boarding"),

        f"{prefix}_destination_stop_id": leg.get("destination_stop_id"),
        f"{prefix}_destination_station": leg.get("destination_station"),
        f"{prefix}_destination_for_alighting": leg.get("destination_for_alighting"),

        f"{prefix}_departure_time": leg.get("departure_time"),
        f"{prefix}_departure_minutes_abs": leg.get("departure_minutes_abs"),
        f"{prefix}_departure_hhmm_abs": minutes_to_hhmm(leg.get("departure_minutes_abs")),

        f"{prefix}_arrival_time": leg.get("arrival_time"),
        f"{prefix}_arrival_minutes_abs": leg.get("arrival_minutes_abs"),
        f"{prefix}_arrival_hhmm_abs": minutes_to_hhmm(leg.get("arrival_minutes_abs")),

        f"{prefix}_duration_min": leg.get("duration_min"),
    }


def path_to_record(origin_stop_id, origin_station, path):
    first = path[0]
    last = path[-1]

    n_legs = len(path)
    n_changes = n_legs - 1

    if n_changes == 0:
        solution_type = "DIRECT"
    elif n_changes == 1:
        solution_type = "ONE_CHANGE"
    elif n_changes == 2:
        solution_type = "TWO_CHANGES"
    else:
        solution_type = f"{n_changes}_CHANGES"

    rec = {
        "origin_stop_id": origin_stop_id,
        "origin_station": origin_station,

        "destination_stop_id": last["destination_stop_id"],
        "destination_station": last["destination_station"],

        "n_legs": n_legs,
        "n_changes": n_changes,
        "solution_type": solution_type,

        "departure_time": first["departure_time"],
        "departure_minutes_abs": first["departure_minutes_abs"],
        "departure_hhmm_abs": minutes_to_hhmm(first["departure_minutes_abs"]),

        "arrival_time": last["arrival_time"],
        "arrival_minutes_abs": last["arrival_minutes_abs"],
        "arrival_hhmm_abs": minutes_to_hhmm(last["arrival_minutes_abs"]),

        "total_duration_min": last["arrival_minutes_abs"] - first["departure_minutes_abs"],

        "path_stations": " -> ".join(
            [path[0]["origin_station"]] + [leg["destination_station"] for leg in path]
        ),

        "path_stop_ids": " -> ".join(
            [path[0]["origin_stop_id"]] + [leg["destination_stop_id"] for leg in path]
        ),

        "path_trains": " | ".join(
            [
                str(leg.get("journey_name") or leg.get("service_journey_id") or "")
                for leg in path
            ]
        ),
    }

    # massimo previsto: 3 tratte = 2 cambi
    for i in range(3):
        prefix = f"leg{i + 1}"
        if i < len(path):
            rec.update(leg_to_record(path[i], prefix))
        else:
            rec.update({
                f"{prefix}_service_journey_id": pd.NA,
                f"{prefix}_journey_name": pd.NA,
                f"{prefix}_line_name": pd.NA,
                f"{prefix}_day_type_ref": pd.NA,

                f"{prefix}_origin_stop_id": pd.NA,
                f"{prefix}_origin_station": pd.NA,
                f"{prefix}_origin_for_boarding": pd.NA,

                f"{prefix}_destination_stop_id": pd.NA,
                f"{prefix}_destination_station": pd.NA,
                f"{prefix}_destination_for_alighting": pd.NA,

                f"{prefix}_departure_time": pd.NA,
                f"{prefix}_departure_minutes_abs": pd.NA,
                f"{prefix}_departure_hhmm_abs": pd.NA,

                f"{prefix}_arrival_time": pd.NA,
                f"{prefix}_arrival_minutes_abs": pd.NA,
                f"{prefix}_arrival_hhmm_abs": pd.NA,

                f"{prefix}_duration_min": pd.NA,
            })

    return rec


# =========================================================
# ALGORITMO RAGGIUNGIBILITÀ
# =========================================================

def trova_raggiungibili_da_origine(
    trips,
    departures_by_stop,
    dep_times_by_stop,
    origin_stop_id,
    origin_station,
    start_min,
    latest_boarding_min,
    min_change_min,
    max_changes,
    keep_all_paths=False
):
    """
    Ricerca breadth-first temporale.

    Regole:
      - primo treno: partenza >= start_min e <= latest_boarding_min
      - cambi: partenza >= arrivo precedente + min_change_min
      - tutti gli imbarchi devono avvenire <= latest_boarding_min
      - l'arrivo finale può essere dopo latest_boarding_min
      - massimo max_changes cambi
      - rispetto for_boarding e for_alighting
    """

    max_legs = max_changes + 1

    queue_states = deque()
    queue_states.append({
        "current_stop_id": origin_stop_id,
        "current_station": origin_station,
        "available_time": start_min,
        "path": [],
        "visited_stops": {origin_stop_id},
    })

    all_records = []

    # Evita riespansioni inutili:
    # se arrivo allo stesso stop con lo stesso numero di tratte a un orario peggiore,
    # non espando quello stato.
    best_state_time = {}

    while queue_states:
        state = queue_states.popleft()

        current_stop_id = state["current_stop_id"]
        available_time = state["available_time"]
        path = state["path"]
        visited_stops = state["visited_stops"]

        used_legs = len(path)

        if used_legs >= max_legs:
            continue

        departures = departures_by_stop.get(current_stop_id, [])
        dep_times = dep_times_by_stop.get(current_stop_id, [])

        if not departures:
            continue

        start_idx = bisect_left(dep_times, available_time)

        for k in range(start_idx, len(departures)):
            dep_i, sj_id, pos_i = departures[k]

            # Vincolo fondamentale: non salgo su treni dopo l'ultima partenza ammessa
            if dep_i > latest_boarding_min:
                break

            trip_rows = trips.get(sj_id)
            if not trip_rows:
                continue

            origin_row = trip_rows[pos_i]

            # Posso salire solo se la fermata consente boarding
            if "for_boarding" in origin_row:
                if not flag_netex_true_or_empty(origin_row.get("for_boarding")):
                    continue

            # Tutte le fermate successive della stessa corsa
            for pos_j in range(pos_i + 1, len(trip_rows)):
                dest_row = trip_rows[pos_j]

                # Posso scendere solo se la fermata consente alighting
                if "for_alighting" in dest_row:
                    if not flag_netex_true_or_empty(dest_row.get("for_alighting")):
                        continue

                dest_stop_id = dest_row.get("stop_place_ref")
                dest_station = dest_row.get("station_name")
                arr_j = dest_row.get("event_arr")

                if pd.isna(dest_stop_id) or pd.isna(dest_station) or pd.isna(arr_j):
                    continue

                if dest_stop_id == current_stop_id:
                    continue

                # Evita cicli nel singolo percorso
                if dest_stop_id in visited_stops:
                    continue

                if float(arr_j) < float(dep_i):
                    continue

                leg = {
                    "service_journey_id": sj_id,
                    "journey_name": origin_row.get("journey_name"),
                    "line_name": origin_row.get("line_name"),
                    "day_type_ref": origin_row.get("day_type_ref"),

                    "origin_stop_id": current_stop_id,
                    "origin_station": origin_row.get("station_name"),
                    "origin_for_boarding": origin_row.get("for_boarding"),

                    "destination_stop_id": dest_stop_id,
                    "destination_station": dest_station,
                    "destination_for_alighting": dest_row.get("for_alighting"),

                    "departure_time": origin_row.get("departure_time"),
                    "departure_minutes_abs": float(dep_i),

                    "arrival_time": dest_row.get("arrival_time"),
                    "arrival_minutes_abs": float(arr_j),

                    "duration_min": float(arr_j) - float(dep_i),
                }

                new_path = path + [leg]
                rec = path_to_record(origin_stop_id, origin_station, new_path)
                all_records.append(rec)

                # Posso espandere ancora se ho tratte residue
                if len(new_path) < max_legs:
                    next_available_time = float(arr_j) + min_change_min

                    # Se dopo il cambio sarei già oltre l'ultima partenza ammessa, non espando
                    if next_available_time > latest_boarding_min:
                        continue

                    state_key = (dest_stop_id, len(new_path))
                    previous_best = best_state_time.get(state_key)

                    if previous_best is not None and previous_best <= next_available_time:
                        continue

                    best_state_time[state_key] = next_available_time

                    queue_states.append({
                        "current_stop_id": dest_stop_id,
                        "current_station": dest_station,
                        "available_time": next_available_time,
                        "path": new_path,
                        "visited_stops": visited_stops | {dest_stop_id},
                    })

    if not all_records:
        return pd.DataFrame(), pd.DataFrame()

    all_paths = pd.DataFrame(all_records).drop_duplicates()

    # Migliore soluzione per destinazione:
    # 1) meno cambi
    # 2) arrivo più precoce
    # 3) durata minore
    # 4) partenza più precoce
    #
    # Se vuoi privilegiare l'arrivo più precoce rispetto al numero di cambi,
    # inverti le prime due colonne dell'ordinamento.
    all_paths = all_paths.sort_values(
        [
            "destination_stop_id",
            "n_changes",
            "arrival_minutes_abs",
            "total_duration_min",
            "departure_minutes_abs",
        ],
        kind="stable"
    )

    best = (
        all_paths
        .groupby("destination_stop_id", as_index=False)
        .first()
    )

    counts = (
        all_paths
        .groupby("destination_stop_id", as_index=False)
        .size()
        .rename(columns={"size": "n_paths_found"})
    )

    best = best.merge(counts, on="destination_stop_id", how="left")

    best = best.sort_values(
        ["n_changes", "arrival_minutes_abs", "destination_station"],
        kind="stable"
    )

    if keep_all_paths:
        return all_paths, best

    return pd.DataFrame(), best


# =========================================================
# PROCEDURA BATCH
# =========================================================

def run_batch(
    journey_passages_csv,
    base_netex_dir,
    output_dir,
    data_viaggio,
    ora_inizio,
    ora_ultima_partenza,
    min_change_min,
    max_changes,
    skip_existing,
    keep_all_paths,
    log,
    progress_callback,
    stop_requested_callback
):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    start_min = parse_time_hhmmss(ora_inizio)
    latest_boarding_min = parse_time_hhmmss(ora_ultima_partenza)

    if start_min is None:
        raise ValueError("Ora inizio non valida. Usa formato HH:MM:SS, es. 07:00:00.")

    if latest_boarding_min is None:
        raise ValueError("Ora ultima partenza non valida. Usa formato HH:MM:SS, es. 08:00:00.")

    if latest_boarding_min < start_min:
        raise ValueError("L'ora ultima partenza deve essere successiva o uguale all'ora inizio.")

    df = carica_journey_passages(journey_passages_csv, log=log)

    df = filtra_journey_passages_per_data(
        df=df,
        base_netex_dir=base_netex_dir,
        data_viaggio=data_viaggio,
        log=log
    )

    trips, departures_by_stop, dep_times_by_stop, station_registry = costruisci_indici(df, log=log)

    total = len(station_registry)
    done = 0
    skipped = 0
    empty = 0
    errors = 0

    log("")
    log("Parametri elaborazione:")
    log(f"  Data viaggio: {data_viaggio}")
    log(f"  Ora inizio: {ora_inizio}")
    log(f"  Ultima partenza ammessa: {ora_ultima_partenza}")
    log(f"  Tempo minimo cambio: {min_change_min} minuti")
    log(f"  Max cambi: {max_changes}")
    log(f"  Stazioni origine da elaborare: {total:,}")
    log("")

    summary_rows = []

    for idx, row in enumerate(station_registry.itertuples(index=False), start=1):
        if stop_requested_callback():
            log("Interruzione richiesta dall'utente.")
            break

        origin_stop_id = row.stop_id
        origin_station = row.station_name

        out_file = output_dir / make_output_filename(origin_stop_id, origin_station)

        progress_callback(idx - 1, total)

        if skip_existing and out_file.exists():
            skipped += 1
            if idx % 25 == 0 or idx == 1:
                log(f"[{idx}/{total}] SKIP già esiste: {out_file.name}")
            continue

        try:
            if idx % 10 == 0 or idx == 1:
                log(f"[{idx}/{total}] Elaboro: {origin_stop_id} - {origin_station}")

            all_paths, best = trova_raggiungibili_da_origine(
                trips=trips,
                departures_by_stop=departures_by_stop,
                dep_times_by_stop=dep_times_by_stop,
                origin_stop_id=origin_stop_id,
                origin_station=origin_station,
                start_min=start_min,
                latest_boarding_min=latest_boarding_min,
                min_change_min=min_change_min,
                max_changes=max_changes,
                keep_all_paths=keep_all_paths
            )

            # Salvo sempre un file best, anche se vuoto,
            # così al rilancio viene saltato se l'opzione è attiva.
            best.to_csv(out_file, index=False, encoding="utf-8-sig")

            if keep_all_paths and not all_paths.empty:
                all_file = output_dir / out_file.name.replace("_best.csv", "_all_paths.csv")
                all_paths.to_csv(all_file, index=False, encoding="utf-8-sig")

            if best.empty:
                empty += 1
            else:
                done += 1

            n_direct = 0
            n_one = 0
            n_two = 0

            if not best.empty and "n_changes" in best.columns:
                n_direct = int((best["n_changes"] == 0).sum())
                n_one = int((best["n_changes"] == 1).sum())
                n_two = int((best["n_changes"] == 2).sum())

            summary_rows.append({
                "origin_stop_id": origin_stop_id,
                "origin_station": origin_station,
                "output_file": out_file.name,
                "n_reachable_stations": len(best),
                "n_direct": n_direct,
                "n_one_change": n_one,
                "n_two_changes": n_two,
                "status": "OK"
            })

        except Exception as e:
            errors += 1
            log(f"ERRORE su {origin_stop_id} - {origin_station}: {e}")
            summary_rows.append({
                "origin_stop_id": origin_stop_id,
                "origin_station": origin_station,
                "output_file": out_file.name,
                "n_reachable_stations": pd.NA,
                "n_direct": pd.NA,
                "n_one_change": pd.NA,
                "n_two_changes": pd.NA,
                "status": f"ERROR: {e}"
            })

    progress_callback(total, total)

    summary = pd.DataFrame(summary_rows)
    summary_file = output_dir / "_riepilogo_raggiungibilita.csv"
    summary.to_csv(summary_file, index=False, encoding="utf-8-sig")

    log("")
    log("FINE ELABORAZIONE")
    log(f"File creati con almeno una destinazione: {done:,}")
    log(f"File vuoti creati: {empty:,}")
    log(f"File saltati perché già esistenti: {skipped:,}")
    log(f"Errori: {errors:,}")
    log(f"Riepilogo: {summary_file}")


# =========================================================
# GUI TKINTER
# =========================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Raggiungibilità ferroviaria NeTEx - max 2 cambi")
        self.geometry("1080x760")

        self.worker_thread = None
        self.msg_queue = queue.Queue()
        self.stop_requested = False

        self._build_ui()
        self.after(200, self._process_queue)

    def _build_ui(self):
        pad = 8

        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        # ---------------- INPUT ----------------
        box_input = ttk.LabelFrame(main, text="Input / Output")
        box_input.pack(fill="x", padx=pad, pady=pad)

        ttk.Label(box_input, text="journey_passages.csv").grid(
            row=0, column=0, sticky="w", padx=pad, pady=pad
        )
        self.var_journey = tk.StringVar(value=DEFAULT_JOURNEY_PASSAGES_CSV)
        ttk.Entry(box_input, textvariable=self.var_journey, width=100).grid(
            row=0, column=1, sticky="we", padx=pad, pady=pad
        )
        ttk.Button(box_input, text="Scegli file", command=self.choose_journey_file).grid(
            row=0, column=2, padx=pad, pady=pad
        )

        ttk.Label(box_input, text="Cartella CSV base NeTEx").grid(
            row=1, column=0, sticky="w", padx=pad, pady=pad
        )
        self.var_base_netex = tk.StringVar(value=DEFAULT_BASE_NETEX_DIR)
        ttk.Entry(box_input, textvariable=self.var_base_netex, width=100).grid(
            row=1, column=1, sticky="we", padx=pad, pady=pad
        )
        ttk.Button(box_input, text="Scegli cartella", command=self.choose_base_netex_dir).grid(
            row=1, column=2, padx=pad, pady=pad
        )

        ttk.Label(box_input, text="Cartella output").grid(
            row=2, column=0, sticky="w", padx=pad, pady=pad
        )
        self.var_outdir = tk.StringVar(value=DEFAULT_OUTPUT_DIR)
        ttk.Entry(box_input, textvariable=self.var_outdir, width=100).grid(
            row=2, column=1, sticky="we", padx=pad, pady=pad
        )
        ttk.Button(box_input, text="Scegli cartella", command=self.choose_output_dir).grid(
            row=2, column=2, padx=pad, pady=pad
        )

        box_input.columnconfigure(1, weight=1)

        # ---------------- PARAMETRI ----------------
        box_par = ttk.LabelFrame(main, text="Parametri")
        box_par.pack(fill="x", padx=pad, pady=pad)

        ttk.Label(box_par, text="Data viaggio, YYYY-MM-DD").grid(
            row=0, column=0, sticky="w", padx=pad, pady=pad
        )
        self.var_travel_date = tk.StringVar(value="2026-05-27")
        ttk.Entry(box_par, textvariable=self.var_travel_date, width=15).grid(
            row=0, column=1, sticky="w", padx=pad, pady=pad
        )

        ttk.Label(box_par, text="Ora inizio").grid(
            row=0, column=2, sticky="w", padx=pad, pady=pad
        )
        self.var_start = tk.StringVar(value="07:00:00")
        ttk.Entry(box_par, textvariable=self.var_start, width=15).grid(
            row=0, column=3, sticky="w", padx=pad, pady=pad
        )

        ttk.Label(box_par, text="Ultima partenza ammessa").grid(
            row=0, column=4, sticky="w", padx=pad, pady=pad
        )
        self.var_latest = tk.StringVar(value="08:00:00")
        ttk.Entry(box_par, textvariable=self.var_latest, width=15).grid(
            row=0, column=5, sticky="w", padx=pad, pady=pad
        )

        ttk.Label(box_par, text="Tempo minimo cambio, minuti").grid(
            row=1, column=0, sticky="w", padx=pad, pady=pad
        )
        self.var_change = tk.StringVar(value="10")
        ttk.Entry(box_par, textvariable=self.var_change, width=15).grid(
            row=1, column=1, sticky="w", padx=pad, pady=pad
        )

        ttk.Label(box_par, text="Max cambi").grid(
            row=1, column=2, sticky="w", padx=pad, pady=pad
        )
        self.var_max_changes = tk.StringVar(value="2")
        ttk.Combobox(
            box_par,
            textvariable=self.var_max_changes,
            values=["0", "1", "2"],
            width=12,
            state="readonly"
        ).grid(row=1, column=3, sticky="w", padx=pad, pady=pad)

        self.var_skip = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            box_par,
            text="Salta le stazioni già elaborate",
            variable=self.var_skip
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=pad, pady=pad)

        self.var_all_paths = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            box_par,
            text="Salva anche tutti i percorsi alternativi (_all_paths.csv)",
            variable=self.var_all_paths
        ).grid(row=2, column=2, columnspan=4, sticky="w", padx=pad, pady=pad)

        # ---------------- COMANDI ----------------
        box_cmd = ttk.Frame(main)
        box_cmd.pack(fill="x", padx=pad, pady=pad)

        self.btn_start = ttk.Button(box_cmd, text="Avvia elaborazione", command=self.start_processing)
        self.btn_start.pack(side="left", padx=pad)

        self.btn_stop = ttk.Button(box_cmd, text="Interrompi", command=self.request_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=pad)

        self.btn_clear = ttk.Button(box_cmd, text="Pulisci log", command=self.clear_log)
        self.btn_clear.pack(side="left", padx=pad)

        # ---------------- PROGRESS ----------------
        box_prog = ttk.LabelFrame(main, text="Avanzamento")
        box_prog.pack(fill="x", padx=pad, pady=pad)

        self.progress = ttk.Progressbar(box_prog, orient="horizontal", mode="determinate")
        self.progress.pack(fill="x", padx=pad, pady=pad)

        self.var_progress_text = tk.StringVar(value="Pronto.")
        ttk.Label(box_prog, textvariable=self.var_progress_text).pack(
            anchor="w", padx=pad, pady=(0, pad)
        )

        # ---------------- LOG ----------------
        box_log = ttk.LabelFrame(main, text="Log")
        box_log.pack(fill="both", expand=True, padx=pad, pady=pad)

        self.txt_log = tk.Text(box_log, wrap="word", height=20)
        self.txt_log.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(box_log, orient="vertical", command=self.txt_log.yview)
        scrollbar.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=scrollbar.set)

        self.log("Procedura pronta.")
        self.log("Output previsto: un CSV best per ogni stazione origine.")
        self.log(
            "Regola temporale: tutti gli imbarchi devono avvenire entro l'ultima partenza ammessa; "
            "l'arrivo può avvenire dopo."
        )
        self.log("Filtro attivo: data viaggio + for_boarding/for_alighting.")

    def choose_journey_file(self):
        path = filedialog.askopenfilename(
            title="Seleziona journey_passages.csv",
            filetypes=[("CSV", "*.csv"), ("Tutti i file", "*.*")]
        )
        if path:
            self.var_journey.set(path)

    def choose_base_netex_dir(self):
        path = filedialog.askdirectory(title="Seleziona cartella CSV base NeTEx")
        if path:
            self.var_base_netex.set(path)

    def choose_output_dir(self):
        path = filedialog.askdirectory(title="Seleziona cartella output")
        if path:
            self.var_outdir.set(path)

    def log(self, msg):
        self.txt_log.insert("end", str(msg) + "\n")
        self.txt_log.see("end")
        self.update_idletasks()

    def clear_log(self):
        self.txt_log.delete("1.0", "end")

    def request_stop(self):
        self.stop_requested = True
        self.log("Richiesta interruzione ricevuta. La procedura si fermerà al termine della stazione corrente.")

    def _log_from_worker(self, msg):
        self.msg_queue.put(("log", msg))

    def _progress_from_worker(self, current, total):
        self.msg_queue.put(("progress", current, total))

    def _stop_requested_from_worker(self):
        return self.stop_requested

    def _process_queue(self):
        try:
            while True:
                item = self.msg_queue.get_nowait()

                if item[0] == "log":
                    self.log(item[1])

                elif item[0] == "progress":
                    current, total = item[1], item[2]
                    if total > 0:
                        self.progress["maximum"] = total
                        self.progress["value"] = current
                        self.var_progress_text.set(f"{current:,} / {total:,} stazioni")

                elif item[0] == "done":
                    self._set_running(False)
                    self.var_progress_text.set("Elaborazione conclusa.")
                    messagebox.showinfo("Fine", "Elaborazione conclusa.")

                elif item[0] == "error":
                    self._set_running(False)
                    err = item[1]
                    self.log(err)
                    messagebox.showerror("Errore", err)

        except queue.Empty:
            pass

        self.after(200, self._process_queue)

    def _set_running(self, running):
        if running:
            self.btn_start.configure(state="disabled")
            self.btn_stop.configure(state="normal")
        else:
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")

    def start_processing(self):
        journey = self.var_journey.get().strip()
        base_netex = self.var_base_netex.get().strip()
        outdir = self.var_outdir.get().strip()

        if not journey:
            messagebox.showerror("Errore", "Indica il file journey_passages.csv.")
            return

        if not Path(journey).exists():
            messagebox.showerror("Errore", f"File non trovato:\n{journey}")
            return

        if not base_netex:
            messagebox.showerror("Errore", "Indica la cartella CSV base NeTEx.")
            return

        if not Path(base_netex).exists():
            messagebox.showerror("Errore", f"Cartella CSV base NeTEx non trovata:\n{base_netex}")
            return

        if not outdir:
            messagebox.showerror("Errore", "Indica la cartella di output.")
            return

        try:
            min_change = int(self.var_change.get())
            max_changes = int(self.var_max_changes.get())
        except Exception:
            messagebox.showerror("Errore", "Tempo minimo cambio e max cambi devono essere numerici.")
            return

        self.stop_requested = False
        self._set_running(True)
        self.progress["value"] = 0
        self.var_progress_text.set("Avvio...")

        args = {
            "journey_passages_csv": journey,
            "base_netex_dir": base_netex,
            "output_dir": outdir,
            "data_viaggio": self.var_travel_date.get().strip(),
            "ora_inizio": self.var_start.get().strip(),
            "ora_ultima_partenza": self.var_latest.get().strip(),
            "min_change_min": min_change,
            "max_changes": max_changes,
            "skip_existing": self.var_skip.get(),
            "keep_all_paths": self.var_all_paths.get(),
            "log": self._log_from_worker,
            "progress_callback": self._progress_from_worker,
            "stop_requested_callback": self._stop_requested_from_worker,
        }

        self.worker_thread = threading.Thread(
            target=self._worker,
            kwargs=args,
            daemon=True
        )
        self.worker_thread.start()

    def _worker(self, **kwargs):
        try:
            run_batch(**kwargs)
            self.msg_queue.put(("done",))
        except Exception:
            err = traceback.format_exc()
            self.msg_queue.put(("error", err))


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    app = App()
    app.mainloop()
