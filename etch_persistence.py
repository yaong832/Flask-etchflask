"""식각 HMI 텔레메트리·이벤트 SQLite 영구 저장 (Phase 3.5)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional


class EtchSqliteStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS etch_telemetry (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    equipment_id INTEGER,
                    equipment_state TEXT,
                    alarm_code TEXT,
                    interlock_ok INTEGER,
                    maintenance_mode INTEGER,
                    bench_mode INTEGER,
                    temperature REAL,
                    humidity REAL,
                    pressure_mtorr REAL,
                    vibration_g REAL,
                    access_safe INTEGER,
                    username TEXT,
                    modules_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_etch_telemetry_src_ts
                    ON etch_telemetry(data_source, ts);

                CREATE TABLE IF NOT EXISTS etch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    equipment_state TEXT,
                    alarm_code TEXT,
                    interlock_ok INTEGER,
                    username TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_etch_events_src_ts
                    ON etch_events(data_source, ts);
                """
            )
            conn.commit()

    def insert_telemetry(self, raw: Dict[str, Any], source: str) -> None:
        ts = raw.get('lastUpdate') or datetime.now().isoformat()
        modules = raw.get('modules')
        modules_json = json.dumps(modules, ensure_ascii=False) if modules else None
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO etch_telemetry (
                    ts, data_source, equipment_id, equipment_state, alarm_code,
                    interlock_ok, maintenance_mode, bench_mode,
                    temperature, humidity, pressure_mtorr, vibration_g,
                    access_safe, username, modules_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    ts,
                    source,
                    raw.get('equipmentId', 1),
                    raw.get('equipmentState'),
                    raw.get('alarmCode'),
                    1 if raw.get('interlockOk') else 0 if raw.get('interlockOk') is False else None,
                    1 if raw.get('maintenanceMode') else 0,
                    1 if raw.get('benchMode') else 0,
                    raw.get('temperature'),
                    raw.get('humidity'),
                    raw.get('pressure'),
                    raw.get('vibration'),
                    1 if raw.get('accessSafe') else 0 if raw.get('accessSafe') is False else None,
                    raw.get('username'),
                    modules_json,
                ),
            )
            conn.commit()

    def insert_event(self, event: Dict[str, Any], source: str = 'live') -> None:
        ts = event.get('time') or event.get('timestamp') or datetime.now().isoformat()
        il = event.get('interlockOk')
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO etch_events (
                    ts, data_source, kind, message,
                    equipment_state, alarm_code, interlock_ok, username
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    ts,
                    event.get('dataSource') or source,
                    event.get('kind') or 'hmi_event',
                    event.get('message') or '',
                    event.get('equipmentState'),
                    event.get('alarmCode'),
                    1 if il is True else 0 if il is False else None,
                    event.get('username'),
                ),
            )
            conn.commit()

    def get_telemetry_history(self, limit: int, source: str) -> List[Dict[str, Any]]:
        n = max(1, min(int(limit), 2500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM etch_telemetry
                WHERE data_source = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (source, n),
            ).fetchall()
        items = []
        for row in reversed(rows):
            items.append(
                {
                    'timestamp': row['ts'],
                    'dataSource': row['data_source'],
                    'equipmentId': row['equipment_id'],
                    'equipmentState': row['equipment_state'],
                    'alarmCode': row['alarm_code'],
                    'interlockOk': bool(row['interlock_ok']) if row['interlock_ok'] is not None else None,
                    'maintenanceMode': bool(row['maintenance_mode']),
                    'benchMode': bool(row['bench_mode']),
                    'temperature': row['temperature'],
                    'humidity': row['humidity'],
                    'pressure_mtorr': row['pressure_mtorr'],
                    'vibration_g': row['vibration_g'],
                    'accessSafe': bool(row['access_safe']) if row['access_safe'] is not None else None,
                    'username': row['username'],
                    'modules': json.loads(row['modules_json']) if row['modules_json'] else None,
                }
            )
        return items

    def get_events(self, limit: int, source: str) -> List[Dict[str, Any]]:
        n = max(1, min(int(limit), 400))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM etch_events
                WHERE data_source = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (source, n),
            ).fetchall()
        return [
            {
                'time': row['ts'],
                'kind': row['kind'],
                'message': row['message'],
                'equipmentState': row['equipment_state'],
                'alarmCode': row['alarm_code'],
                'interlockOk': bool(row['interlock_ok']) if row['interlock_ok'] is not None else None,
                'username': row['username'],
                'dataSource': row['data_source'],
            }
            for row in rows
        ]

    def get_latest_telemetry(self, source: str) -> Optional[Dict[str, Any]]:
        """가장 최근 텔레메트리 1건 (재시작 후 메모리 복원·/api/sensors 폴백용)."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM etch_telemetry
                WHERE data_source = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (source,),
            ).fetchone()
        if not row:
            return None
        return {
            'timestamp': row['ts'],
            'dataSource': row['data_source'],
            'equipmentId': row['equipment_id'],
            'equipmentState': row['equipment_state'],
            'alarmCode': row['alarm_code'],
            'interlockOk': bool(row['interlock_ok']) if row['interlock_ok'] is not None else None,
            'maintenanceMode': bool(row['maintenance_mode']),
            'benchMode': bool(row['bench_mode']),
            'temperature': row['temperature'],
            'humidity': row['humidity'],
            'pressure_mtorr': row['pressure_mtorr'],
            'vibration_g': row['vibration_g'],
            'accessSafe': bool(row['access_safe']) if row['access_safe'] is not None else None,
            'username': row['username'],
            'modules': json.loads(row['modules_json']) if row['modules_json'] else None,
            'connected': False,
            'sensorsLive': False,
        }

    def get_latest_modules(self, source: str) -> tuple[List[Dict[str, Any]], Optional[str]]:
        """가장 최근 modules_json 스냅샷."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT modules_json, ts, equipment_state
                FROM etch_telemetry
                WHERE data_source = ?
                  AND modules_json IS NOT NULL
                  AND TRIM(modules_json) != ''
                  AND modules_json != 'null'
                ORDER BY id DESC
                LIMIT 1
                """,
                (source,),
            ).fetchone()
        if not row:
            return [], None
        try:
            parsed = json.loads(row['modules_json'])
        except (TypeError, json.JSONDecodeError):
            return [], row['ts']
        modules = parsed if isinstance(parsed, list) else []
        return modules, row['ts']

    def summarize(self, source: str) -> Dict[str, Any]:
        with self._connect() as conn:
            count = conn.execute(
                "SELECT COUNT(1) FROM etch_telemetry WHERE data_source = ?",
                (source,),
            ).fetchone()[0]
            last = conn.execute(
                """
                SELECT * FROM etch_telemetry
                WHERE data_source = ?
                ORDER BY id DESC LIMIT 1
                """,
                (source,),
            ).fetchone()
            alarm_ev = conn.execute(
                "SELECT COUNT(1) FROM etch_events WHERE data_source = ? AND kind = 'alarm'",
                (source,),
            ).fetchone()[0]
            il_ev = conn.execute(
                "SELECT COUNT(1) FROM etch_events WHERE data_source = ? AND kind = 'interlock_lost'",
                (source,),
            ).fetchone()[0]
        last_dict = None
        if last:
            last_dict = {
                'timestamp': last['ts'],
                'equipmentState': last['equipment_state'],
                'alarmCode': last['alarm_code'],
            }
        return {
            'samples': count,
            'last': last_dict,
            'alarmEvents': alarm_ev,
            'interlockEvents': il_ev,
            'persisted': True,
            'dbPath': self.db_path,
        }
