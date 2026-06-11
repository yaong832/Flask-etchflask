import sqlite3
import json
import os
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

class DataManager:
    """센서 데이터 및 로그를 관리하는 클래스"""
    
    def __init__(self, db_path='data/smart_farm.db', use_db=True, etch_sqlite_path: Optional[str] = None):
        """
        Args:
            db_path: 데이터베이스 파일 경로
            use_db: DB 사용 여부 (False면 메모리 기반)
            etch_sqlite_path: 식각 텔레메트리·이벤트 영구 DB (Phase 3.5)
        """
        self.use_db = use_db
        self.db_path = db_path if use_db else ':memory:'
        self._etch_store = None
        if etch_sqlite_path:
            from etch_persistence import EtchSqliteStore
            self._etch_store = EtchSqliteStore(etch_sqlite_path)
        self._last_modules_live: Optional[List[Dict[str, Any]]] = None
        self._last_modules_demo: Optional[List[Dict[str, Any]]] = None
        self._last_recipe_live: Optional[Dict[str, Any]] = None
        self._last_recipe_demo: Optional[Dict[str, Any]] = None

        # 메모리 기반일 때는 인메모리 리스트 사용
        if not use_db:
            self.sensor_data_list = []
            self.logs_list = []
            self.production_data_list = []
            # WPF → Flask 식각 텔레메트리 링버퍼(모니터링/리포트용)
            # 실가공(EtherCAT live) 전용 — AI·KPI·웹 차트
            self.etch_history: List[Dict[str, Any]] = []
            self.etch_events: List[Dict[str, Any]] = []
            self._last_etch_equipment_state: Optional[str] = None
            self._last_etch_alarm: Optional[str] = None
            self._last_etch_interlock: Optional[bool] = None
            # 데모(시뮬) 전용 — 실가공 이력과 분리
            self.demo_etch_history: List[Dict[str, Any]] = []
            self.demo_etch_events: List[Dict[str, Any]] = []
            self._last_demo_equipment_state: Optional[str] = None
            self._last_demo_alarm: Optional[str] = None
            self._last_demo_interlock: Optional[bool] = None
            self._demo_stream_active: bool = False
            self._last_demo_status: Optional[Dict[str, Any]] = None
            self._last_ai_diagnosis: Optional[Dict[str, Any]] = None
        else:
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    def initialize_database(self):
        """데이터베이스 초기화 (DB 사용 시에만)"""
        if not self.use_db:
            # 메모리 모드에서는 초기화 불필요
            return
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # 센서 데이터 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sensor_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                farm_id INTEGER NOT NULL,
                humidity REAL,
                temperature REAL,
                light REAL,
                soil_moisture REAL,
                power_on INTEGER,
                connected INTEGER
            )
        ''')
        
        # 로그 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                message TEXT NOT NULL,
                date TEXT,
                log_type TEXT
            )
        ''')
        
        # 농장 정보 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS farm_info (
                farm_id INTEGER PRIMARY KEY,
                crop_name TEXT,
                note TEXT,
                last_updated TEXT
            )
        ''')
        
        # 생산량 데이터 테이블
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS production_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                farm_id INTEGER NOT NULL,
                production_amount REAL,
                unit TEXT
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def validate_sensor_value(self, sensor_key: str, value: float) -> float:
        """
        센서 값 검증 및 정규화
        
        Args:
            sensor_key: 센서 키 ('humidity', 'temperature', 'light', 'soil_moisture')
            value: 검증할 값
        
        Returns:
            검증된 값 (범위를 벗어난 경우 클리핑됨)
        """
        valid_ranges = {
            'humidity': (0, 100),
            'temperature': (-10, 50),
            'light': (0, 100),
            'soil_moisture': (0, 100),
            # 식각 압력(mTorr) — farmui 채광 % 상한(100) 사용 금지
            'pressure_mtorr': (0, 5000),
        }
        
        if sensor_key in valid_ranges:
            min_val, max_val = valid_ranges[sensor_key]
            if value < min_val:
                return min_val
            elif value > max_val:
                return max_val
        return value
    
    def save_sensor_data(self, sensor_data: Dict[str, Any]):
        """센서 데이터 저장 (데이터 검증 포함)"""
        if not self.use_db:
            # 메모리 모드: 리스트에 추가 (최대 1000개 유지)
            timestamp = sensor_data.get('lastUpdate', datetime.now().isoformat())
            farm_id = sensor_data.get('currentFarm', 1)
            power_on = 1 if sensor_data.get('powerOn', False) else 0
            connected = 1 if sensor_data.get('connected', False) else 0
            
            sensors = sensor_data.get('sensors', [])
            sensors_live = bool(sensor_data.get('sensorsLive', False)) and bool(connected)
            
            # 센서 값 추출 및 검증 (EtherCAT 실측일 때만)
            humidity = None
            temperature = None
            light = None
            soil_moisture = None
            
            if not sensors_live:
                sensors = []
            
            for sensor in sensors:
                name = sensor.get('name', '')
                raw_value = sensor.get('rawValue', 0)
                
                # 데이터 검증 및 정규화
                if name == '습도':
                    humidity = self.validate_sensor_value('humidity', raw_value)
                elif name == '온도':
                    temperature = self.validate_sensor_value('temperature', raw_value)
                elif name == '압력':
                    light = self.validate_sensor_value('pressure_mtorr', raw_value)
                elif name == '채광':
                    light = self.validate_sensor_value('light', raw_value)
                elif name in ('토양습도', '진동'):
                    soil_moisture = self.validate_sensor_value('soil_moisture', raw_value)
            
            data_entry = {
                'id': len(self.sensor_data_list) + 1,
                'timestamp': timestamp,
                'farm_id': farm_id,
                'humidity': humidity,
                'temperature': temperature,
                'light': light,
                'soil_moisture': soil_moisture,
                'power_on': power_on,
                'connected': connected,
                'sensors_live': sensors_live,
            }

            for ek in ('equipmentState', 'alarmCode', 'accessSafe', 'interlockOk', 'username'):
                if ek in sensor_data:
                    data_entry[ek] = sensor_data[ek]

            data_entry['data_source'] = sensor_data.get('dataSource', 'live')
            if data_entry['data_source'] != 'live':
                return

            self.sensor_data_list.append(data_entry)
            self._append_etch_history_and_events(data_entry, history=self.etch_history, events=self.etch_events,
                state_attr='_last_etch_equipment_state', alarm_attr='_last_etch_alarm', interlock_attr='_last_etch_interlock')
            
            # 최대 1000개까지만 유지 (오래된 데이터 삭제)
            if len(self.sensor_data_list) > 1000:
                self.sensor_data_list.pop(0)
            
            return
        
        # DB 모드
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            timestamp = sensor_data.get('lastUpdate', datetime.now().isoformat())
            farm_id = sensor_data.get('currentFarm', 1)
            power_on = 1 if sensor_data.get('powerOn', False) else 0
            connected = 1 if sensor_data.get('connected', False) else 0
            
            sensors = sensor_data.get('sensors', [])
            
            # 센서 값 추출
            humidity = None
            temperature = None
            light = None
            soil_moisture = None
            
            for sensor in sensors:
                name = sensor.get('name', '')
                raw_value = sensor.get('rawValue', 0)
                
                if name == '습도':
                    humidity = raw_value
                elif name == '온도':
                    temperature = raw_value
                elif name == '압력':
                    light = self.validate_sensor_value('pressure_mtorr', raw_value)
                elif name == '채광':
                    light = self.validate_sensor_value('light', raw_value)
                elif name in ('토양습도', '진동'):
                    soil_moisture = raw_value
            
            cursor.execute('''
                INSERT INTO sensor_data 
                (timestamp, farm_id, humidity, temperature, light, soil_moisture, power_on, connected)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (timestamp, farm_id, humidity, temperature, light, soil_moisture, power_on, connected))
            
            conn.commit()
        except Exception as e:
            print(f"센서 데이터 저장 오류: {e}")
        finally:
            conn.close()

    def _ring_append(self, buf: List[Dict[str, Any]], item: Dict[str, Any], max_len: int) -> None:
        buf.append(item)
        overflow = len(buf) - max_len
        if overflow > 0:
            del buf[0:overflow]

    @staticmethod
    def resolve_data_source(raw: Dict[str, Any]) -> str:
        """WPF POST JSON → live | demo | offline."""
        explicit = (raw.get('dataSource') or raw.get('data_source') or '').strip().lower()
        if explicit in ('live', 'demo', 'offline'):
            return explicit
        bench = bool(raw.get('benchMode', False))
        plc_connected = bool(raw.get('connected', False))
        sensors_live = bool(raw.get('sensorsLive', False)) and plc_connected
        if sensors_live:
            return 'live'
        if bench:
            return 'demo'
        return 'offline'

    def _cache_recipe(self, raw: Dict[str, Any], source: str) -> None:
        from recipe_engine import normalize_recipe
        recipe = normalize_recipe(raw.get('recipe'))
        if not recipe:
            return
        if source == 'demo':
            self._last_recipe_demo = recipe
        elif source == 'live':
            self._last_recipe_live = recipe

    def get_active_recipe(self, source: str = 'live') -> Optional[Dict[str, Any]]:
        if source == 'demo':
            return dict(self._last_recipe_demo) if self._last_recipe_demo else None
        return dict(self._last_recipe_live) if self._last_recipe_live else None

    def _cache_modules(self, raw: Dict[str, Any], source: str) -> None:
        modules = raw.get('modules')
        if not modules or not isinstance(modules, list):
            return
        if source == 'demo':
            self._last_modules_demo = modules
        elif source == 'live':
            self._last_modules_live = modules

    def get_latest_modules(self, source: str = 'live') -> List[Dict[str, Any]]:
        modules, _ = self.get_latest_modules_meta(source)
        return modules

    def get_latest_modules_meta(self, source: str = 'live') -> tuple[List[Dict[str, Any]], Optional[str]]:
        if source not in ('live', 'demo'):
            source = 'live'
        if self._etch_store:
            modules, ts = self._etch_store.get_latest_modules(source)
            if modules:
                return modules, ts
        cached = self._last_modules_demo if source == 'demo' else self._last_modules_live
        if cached:
            snap = self._last_demo_status if source == 'demo' else self.get_latest_sensor_data()
            ts = (snap or {}).get('lastUpdate') or (snap or {}).get('timestamp')
            return list(cached), ts
        return [], None

    def ingest_etch_post(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """식각 텔레메트리 수신 — 실가공·데모 버퍼 분리."""
        source = self.resolve_data_source(raw)
        if source == 'live':
            self._ingest_live_post(raw)
            self._cache_modules(raw, 'live')
            self._cache_recipe(raw, 'live')
            if self._etch_store:
                self._etch_store.insert_telemetry(raw, 'live')
            return {'dataSource': 'live', 'stored': True, 'aiUpdated': not self.use_db, 'persisted': bool(self._etch_store)}
        if source == 'demo':
            self._ingest_demo_post(raw)
            self._cache_modules(raw, 'demo')
            self._cache_recipe(raw, 'demo')
            if self._etch_store:
                self._etch_store.insert_telemetry(raw, 'demo')
            return {'dataSource': 'demo', 'stored': True, 'aiUpdated': False, 'persisted': bool(self._etch_store)}
        return {'dataSource': 'offline', 'stored': False, 'aiUpdated': False, 'persisted': False}

    def persist_etch_event(self, event: Dict[str, Any], source: Optional[str] = None) -> None:
        """WPF·내부 이벤트 영구 저장."""
        src = (source or event.get('dataSource') or 'live').lower()
        if src not in ('live', 'demo'):
            src = 'live'
        if not self.use_db:
            buf = self.demo_etch_events if src == 'demo' else self.etch_events
            self._ring_append(buf, event, 400)
        if self._etch_store:
            self._etch_store.insert_event(event, src)

    def _ingest_live_post(self, raw: Dict[str, Any]) -> None:
        plc_connected = bool(raw.get('connected', False))
        sensors_live = bool(raw.get('sensorsLive', False)) and plc_connected
        sensor_data = {
            'currentFarm': raw.get('equipmentId', 1),
            'powerOn': raw.get('powerOn', False),
            'connected': plc_connected,
            'sensorsLive': sensors_live,
            'dataSource': 'live',
            'lastUpdate': raw.get('lastUpdate', datetime.now().isoformat()),
            'sensors': [],
            'equipmentState': raw.get('equipmentState'),
            'alarmCode': raw.get('alarmCode'),
            'accessSafe': raw.get('accessSafe'),
            'interlockOk': raw.get('interlockOk'),
            'username': raw.get('username'),
            'modules': raw.get('modules'),
            'benchMode': bool(raw.get('benchMode', False)),
            'dataSource': raw.get('dataSource', 'live'),
            'maintenanceMode': bool(raw.get('maintenanceMode', False)),
        }
        if sensors_live:
            sensor_data['sensors'] = [
                {'name': '온도', 'rawValue': raw.get('temperature', 0)},
                {'name': '습도', 'rawValue': raw.get('humidity', 0)},
                {'name': '압력', 'rawValue': raw.get('pressure', 0)},
                {'name': '진동', 'rawValue': raw.get('vibration', 0)},
            ]
        self.save_sensor_data(sensor_data)

    def _ingest_demo_post(self, raw: Dict[str, Any]) -> None:
        if self.use_db:
            return
        ts = raw.get('lastUpdate', datetime.now().isoformat())
        sample: Dict[str, Any] = {
            'timestamp': ts,
            'dataSource': 'demo',
            'equipmentId': raw.get('equipmentId', 1),
            'temperature': raw.get('temperature'),
            'humidity': raw.get('humidity'),
            'pressure_mtorr': raw.get('pressure'),
            'vibration_g': raw.get('vibration'),
            'equipmentState': raw.get('equipmentState'),
            'alarmCode': raw.get('alarmCode'),
            'interlockOk': raw.get('interlockOk'),
            'accessSafe': raw.get('accessSafe'),
            'username': raw.get('username'),
            'benchMode': True,
            'connected': False,
            'sensorsLive': False,
            'modules': raw.get('modules'),
        }
        self._ring_append(self.demo_etch_history, sample, 1500)
        self._demo_stream_active = True
        self._last_demo_status = dict(sample)

        entry = {
            'timestamp': ts,
            'farm_id': sample['equipmentId'],
            'equipmentState': sample.get('equipmentState'),
            'alarmCode': sample.get('alarmCode'),
            'interlockOk': sample.get('interlockOk'),
            'username': sample.get('username'),
        }
        self._append_etch_history_and_events(
            entry,
            history=self.demo_etch_history,
            events=self.demo_etch_events,
            state_attr='_last_demo_equipment_state',
            alarm_attr='_last_demo_alarm',
            interlock_attr='_last_demo_interlock',
            skip_history_sample=True,
        )

    def _append_etch_history_and_events(
        self,
        data_entry: Dict[str, Any],
        *,
        history: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        state_attr: str,
        alarm_attr: str,
        interlock_attr: str,
        skip_history_sample: bool = False,
    ) -> None:
        """메모리 모드: 상태·알람·인터록 이벤트 (이력 샘플은 호출측에서 이미 넣었을 수 있음)."""
        if self.use_db:
            return

        ts = data_entry.get('timestamp', datetime.now().isoformat())
        if not skip_history_sample:
            sample: Dict[str, Any] = {
                'timestamp': ts,
                'dataSource': data_entry.get('data_source', 'live'),
                'equipmentId': data_entry.get('farm_id'),
                'temperature': data_entry.get('temperature'),
                'humidity': data_entry.get('humidity'),
                'pressure_mtorr': data_entry.get('light'),
                'vibration_g': data_entry.get('soil_moisture'),
                'equipmentState': data_entry.get('equipmentState'),
                'alarmCode': data_entry.get('alarmCode'),
                'interlockOk': data_entry.get('interlockOk'),
                'accessSafe': data_entry.get('accessSafe'),
                'username': data_entry.get('username'),
                'connected': bool(data_entry.get('connected')),
                'powerOn': bool(data_entry.get('power_on')),
            }
            self._ring_append(history, sample, 2500)

        st = data_entry.get('equipmentState')
        al = data_entry.get('alarmCode')
        il = data_entry.get('interlockOk')
        user = data_entry.get('username')

        def ev(kind: str, message: str) -> Dict[str, Any]:
            return {
                'time': ts,
                'kind': kind,
                'message': message,
                'equipmentState': st,
                'alarmCode': al,
                'interlockOk': il,
                'username': user,
            }

        if st is not None:
            prev_st = getattr(self, state_attr)
            if prev_st is not None and str(st) != prev_st:
                self._ring_append(events, ev('state_change', f"상태 {prev_st} → {st}"), 400)
            setattr(self, state_attr, str(st))

        prev_al = getattr(self, alarm_attr)
        cur_al = str(al) if al else None
        if cur_al != prev_al:
            if al:
                self._ring_append(events, ev('alarm', f"알람: {al}"), 400)
            elif prev_al:
                self._ring_append(events, ev('alarm', f"알람 해제 (이전 {prev_al})"), 400)
            setattr(self, alarm_attr, cur_al)

        if isinstance(il, bool):
            prev_il = getattr(self, interlock_attr)
            if prev_il is True and il is False:
                self._ring_append(events, ev('interlock_lost', '인터록 미충족'), 400)
            elif prev_il is False and il is True:
                self._ring_append(events, ev('interlock_ok', '인터록 충족'), 400)
            setattr(self, interlock_attr, il)

    def set_ai_diagnosis(self, result: Dict[str, Any]) -> None:
        if self.use_db:
            return
        entry = dict(result)
        entry['updatedAt'] = datetime.now().isoformat()
        self._last_ai_diagnosis = entry

    def get_ai_diagnosis_latest(self) -> Optional[Dict[str, Any]]:
        if self.use_db:
            return None
        return self._last_ai_diagnosis

    def _module_counts_from_list(self, modules: Optional[List[Dict[str, Any]]]) -> tuple[int, int, int, int]:
        mod_run = mod_alarm = mod_proc = mod_chamber = 0
        if not isinstance(modules, list):
            return mod_run, mod_alarm, mod_proc, mod_chamber
        for m in modules:
            if not isinstance(m, dict):
                continue
            st = str(m.get('state') or '').upper()
            mid = str(m.get('id') or '').upper()
            if st == 'RUNNING':
                mod_run += 1
            if st == 'ALARM':
                mod_alarm += 1
            if st == 'PROCESSING':
                mod_proc += 1
                if mid in ('PM1', 'PM2', 'PM3', 'PM4'):
                    mod_chamber += 1
        return mod_run, mod_alarm, mod_proc, mod_chamber

    def _build_ai_input_from_telemetry(self, row: Dict[str, Any]) -> Dict[str, Any]:
        modules = row.get('modules') or []
        mod_run, mod_alarm, mod_proc, mod_chamber = self._module_counts_from_list(modules)
        return {
            'equipmentState': row.get('equipmentState'),
            'alarmCode': row.get('alarmCode'),
            'interlockOk': row.get('interlockOk'),
            'accessSafe': row.get('accessSafe'),
            'temperature': row.get('temperature'),
            'humidity': row.get('humidity'),
            'pressure': row.get('pressure_mtorr'),
            'vibration': row.get('vibration_g'),
            'sensorsLive': False,
            'benchMode': bool(row.get('benchMode')),
            'modules': modules,
            'moduleRunningCount': mod_run,
            'moduleAlarmCount': mod_alarm,
            'moduleProcessingCount': mod_proc,
            'chamberProcessingCount': mod_chamber,
        }

    def _persisted_sensor_snapshot(self) -> Optional[Dict[str, Any]]:
        """재시작 직후 메모리가 비었을 때 SQLite 최신 스냅샷으로 /api/sensors 폴백."""
        if not self._etch_store:
            return None
        for src in ('demo', 'live'):
            row = self._etch_store.get_latest_telemetry(src)
            if not row:
                continue
            if src == 'demo':
                return self._snapshot_from_demo(row)
        return None

    def hydrate_from_persisted(self) -> None:
        """재시작 후 SQLite 텔레메트리·모듈·AI 진단을 메모리에 복원."""
        if self.use_db or not self._etch_store:
            return
        for src, modules_attr in (('demo', '_last_modules_demo'), ('live', '_last_modules_live')):
            row = self._etch_store.get_latest_telemetry(src)
            if not row:
                continue
            if src == 'demo':
                self._last_demo_status = dict(row)
                self._demo_stream_active = True
            modules = row.get('modules')
            if modules:
                setattr(self, modules_attr, modules)
        row = self._last_demo_status or self._etch_store.get_latest_telemetry('live')
        if not row:
            return
        try:
            from etch_ai import etch_ai_predict as run_predict
            from etch_config import PRESSURE_INTERLOCK_MIN, PRESSURE_INTERLOCK_MAX

            ai_input = self._build_ai_input_from_telemetry(row)
            ai_input['pressureMin'] = PRESSURE_INTERLOCK_MIN
            ai_input['pressureMax'] = PRESSURE_INTERLOCK_MAX
            ai_input['vibrationMax'] = 0.8
            self.set_ai_diagnosis(run_predict(ai_input))
        except Exception:
            pass

    def get_etch_telemetry_history(self, limit: int = 500, source: str = 'live') -> List[Dict[str, Any]]:
        if self._etch_store:
            return self._etch_store.get_telemetry_history(limit, source)
        if self.use_db:
            return []
        n = max(1, min(int(limit), 2500))
        buf = self.demo_etch_history if source == 'demo' else self.etch_history
        return list(buf[-n:])

    def get_etch_events(self, limit: int = 100, source: str = 'live') -> List[Dict[str, Any]]:
        if self._etch_store:
            return self._etch_store.get_events(limit, source)
        if self.use_db:
            return []
        n = max(1, min(int(limit), 400))
        buf = self.demo_etch_events if source == 'demo' else self.etch_events
        return list(buf[-n:])[::-1]

    def _summarize_history(self, hist: List[Dict[str, Any]], events: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not hist:
            return {
                'samples': 0,
                'last': None,
                'runningRatio': None,
                'alarmEvents': 0,
                'interlockEvents': 0,
            }
        last = dict(hist[-1])
        total = len(hist)
        run_ct = sum(
            1
            for s in hist
            if str(s.get('equipmentState') or '').upper() == 'RUNNING'
        )
        alarm_ev = sum(1 for e in events if e.get('kind') == 'alarm')
        il_ev = sum(1 for e in events if e.get('kind') == 'interlock_lost')
        return {
            'samples': total,
            'last': last,
            'runningRatio': round(run_ct / total, 4) if total else None,
            'alarmEvents': alarm_ev,
            'interlockEvents': il_ev,
        }

    def get_etch_summary(self, source: str = 'live') -> Dict[str, Any]:
        """브라우저용 KPI — 기본은 실가공(live) 이력만."""
        if self._etch_store:
            base = self._etch_store.summarize(source)
            if not self.use_db and source == 'demo':
                base['demoSamples'] = len(self.demo_etch_history)
            elif not self.use_db:
                base['liveSamples'] = len(self.etch_history)
        elif self.use_db:
            base = {
                'samples': 0,
                'last': None,
                'runningRatio': None,
                'alarmEvents': 0,
                'interlockEvents': 0,
            }
        elif source == 'demo':
            base = self._summarize_history(self.demo_etch_history, self.demo_etch_events)
        else:
            base = self._summarize_history(self.etch_history, self.etch_events)

        if not self.use_db:
            base['dataSource'] = source
            base['demoStreamActive'] = self._demo_stream_active
            base['demoSamples'] = len(self.demo_etch_history)
            base['liveSamples'] = len(self.etch_history)
            if self._last_demo_status:
                base['lastDemo'] = dict(self._last_demo_status)
        return base
    
    def _row_sensors_live(self, row_data: Dict[str, Any]) -> bool:
        return bool(row_data.get('sensors_live')) and bool(row_data.get('connected'))

    def _snapshot_from_demo(self, demo: Dict[str, Any]) -> Dict[str, Any]:
        """데모 버퍼 → /api/sensors 스냅샷 (WPF 시뮬·웹 KPI)."""
        ts = demo.get('timestamp', datetime.now().isoformat())
        sensors: List[Dict[str, Any]] = []
        if demo.get('temperature') is not None:
            t = float(demo['temperature'])
            sensors.append({
                'id': 2, 'name': '온도', 'value': f'{t:.1f}℃',
                'rawValue': t, 'percentage': int(t), 'status': '정상',
            })
        if demo.get('humidity') is not None:
            h = float(demo['humidity'])
            sensors.append({
                'id': 1, 'name': '습도', 'value': f'{h:.1f}%',
                'rawValue': h, 'percentage': int(h), 'status': '정상',
            })
        if demo.get('pressure_mtorr') is not None:
            p = float(demo['pressure_mtorr'])
            sensors.append({
                'id': 3, 'name': '압력', 'value': f'{p:.1f} mTorr',
                'rawValue': p, 'percentage': None, 'status': '정상',
            })
        if demo.get('vibration_g') is not None:
            v = float(demo['vibration_g'])
            sensors.append({
                'id': 4, 'name': '진동', 'value': f'{v:.2f} g',
                'rawValue': v, 'percentage': int(v), 'status': '정상',
            })
        return {
            'currentFarm': demo.get('equipmentId', 1),
            'powerOn': True,
            'connected': False,
            'sensorsLive': False,
            'dataSource': 'demo',
            'benchMode': True,
            'lastUpdate': ts,
            'sensors': sensors,
            'equipmentState': demo.get('equipmentState'),
            'alarmCode': demo.get('alarmCode'),
            'accessSafe': demo.get('accessSafe'),
            'interlockOk': demo.get('interlockOk'),
            'username': demo.get('username'),
        }

    def _build_sensor_list_from_row(self, row_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """EtherCAT 실측이 있을 때만 센서 배열 반환 (미연결·시뮬 시 빈 목록)."""
        if not self._row_sensors_live(row_data):
            return []

        sensors: List[Dict[str, Any]] = []
        if row_data.get('humidity') is not None:
            h = row_data['humidity']
            sensors.append({
                'id': 1,
                'name': '습도',
                'value': f'{h:.1f}%',
                'rawValue': h,
                'percentage': int(h),
                'status': '정상',
            })
        if row_data.get('temperature') is not None:
            t = row_data['temperature']
            sensors.append({
                'id': 2,
                'name': '온도',
                'value': f'{t:.1f}℃',
                'rawValue': t,
                'percentage': int(t),
                'status': '정상',
            })
        if row_data.get('light') is not None:
            p = row_data['light']
            sensors.append({
                'id': 3,
                'name': '압력',
                'value': f'{p:.1f} mTorr',
                'rawValue': p,
                'percentage': None,
                'status': '정상',
            })
        if row_data.get('soil_moisture') is not None:
            v = row_data['soil_moisture']
            sensors.append({
                'id': 4,
                'name': '진동',
                'value': f'{v:.2f} g',
                'rawValue': v,
                'percentage': int(v),
                'status': '정상',
            })
        return sensors

    def get_latest_sensor_data(self, farm_id: Optional[int] = None) -> Dict[str, Any]:
        """
        최신 센서 데이터 가져오기 (농장별 필터링 지원)
        
        Args:
            farm_id: 특정 농장 ID (None이면 전체 농장 중 최신 데이터)
        """
        if not self.use_db:
            # 메모리 모드: 최신 데이터 반환
            if not self.sensor_data_list:
                if self._last_demo_status:
                    return self._snapshot_from_demo(self._last_demo_status)
                persisted = self._persisted_sensor_snapshot()
                if persisted:
                    return persisted
                return {
                    "currentFarm": farm_id or 1,
                    "powerOn": False,
                    "connected": False,
                    "sensorsLive": False,
                    "dataSource": "offline",
                    "lastUpdate": datetime.now().isoformat(),
                    "sensors": []
                }
            
            # farm_id가 지정되면 해당 농장의 최신 데이터만 반환
            if farm_id is not None:
                # 역순으로 검색하여 해당 농장의 최신 데이터 찾기
                for row_data in reversed(self.sensor_data_list):
                    if row_data.get('farm_id') == farm_id:
                        break
                else:
                    # 해당 농장 데이터가 없으면 빈 데이터 반환
                    return {
                        "currentFarm": farm_id,
                        "powerOn": False,
                        "connected": False,
                        "sensorsLive": False,
                        "lastUpdate": datetime.now().isoformat(),
                        "sensors": []
                    }
            else:
                row_data = None
                latest_live_ts = ''
                for candidate in reversed(self.sensor_data_list):
                    ds = candidate.get('data_source', 'live')
                    if ds == 'live' or self._row_sensors_live(candidate):
                        row_data = candidate
                        latest_live_ts = candidate.get('timestamp', '')
                        break

                demo_ts = (self._last_demo_status or {}).get('timestamp', '')
                if self._last_demo_status and demo_ts >= latest_live_ts:
                    return self._snapshot_from_demo(self._last_demo_status)

                if row_data is None:
                    if self._last_demo_status:
                        return self._snapshot_from_demo(self._last_demo_status)
                    return {
                        "currentFarm": farm_id or 1,
                        "powerOn": False,
                        "connected": False,
                        "sensorsLive": False,
                        "dataSource": "offline",
                        "lastUpdate": datetime.now().isoformat(),
                        "sensors": []
                    }

            result = {
                "currentFarm": row_data['farm_id'],
                "powerOn": bool(row_data['power_on']),
                "connected": bool(row_data['connected']),
                "sensorsLive": self._row_sensors_live(row_data),
                "dataSource": row_data.get('data_source', 'live'),
                "lastUpdate": row_data['timestamp'],
                "sensors": self._build_sensor_list_from_row(row_data),
            }
            for ek in ('equipmentState', 'alarmCode', 'accessSafe', 'interlockOk', 'username'):
                if ek in row_data:
                    result[ek] = row_data[ek]
            return result
        
        # DB 모드
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            if farm_id is not None:
                # 특정 농장의 최신 데이터만 조회
                cursor.execute('''
                    SELECT * FROM sensor_data 
                    WHERE farm_id = ?
                    ORDER BY timestamp DESC 
                    LIMIT 1
                ''', (farm_id,))
            else:
                # 전체 농장 중 최신 데이터 조회
                cursor.execute('''
                    SELECT * FROM sensor_data 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                ''')
            
            row = cursor.fetchone()
            
            # farm_id가 지정되었는데 데이터가 없으면 빈 데이터 반환
            if not row and farm_id is not None:
                return {
                    "currentFarm": farm_id,
                    "powerOn": False,
                    "connected": False,
                    "lastUpdate": datetime.now().isoformat(),
                    "sensors": []
                }
            
            if row:
                # DB 스키마: id, timestamp, farm_id, humidity, temperature, light, soil_moisture, power_on, connected
                return {
                    "currentFarm": row[2],
                    "powerOn": bool(row[7]),
                    "connected": bool(row[8]),
                    "lastUpdate": row[1],
                    "sensors": [
                        {
                            "id": 1,
                            "name": "습도",
                            "value": f"{row[3]:.1f}%" if row[3] is not None else "0%",
                            "rawValue": row[3] if row[3] is not None else 0,
                            "percentage": int(row[3]) if row[3] is not None else 0,
                            "status": "정상"
                        },
                        {
                            "id": 2,
                            "name": "온도",
                            "value": f"{row[4]:.1f}℃" if row[4] is not None else "0℃",
                            "rawValue": row[4] if row[4] is not None else 0,
                            "percentage": int(row[4]) if row[4] is not None else 0,
                            "status": "정상"
                        },
                        {
                            "id": 3,
                            "name": "압력",
                            "value": f"{row[5]:.1f} mTorr" if row[5] is not None else "— mTorr",
                            "rawValue": row[5] if row[5] is not None else 0,
                            "percentage": int(row[5]) if row[5] is not None else 0,
                            "status": "정상"
                        },
                        {
                            "id": 4,
                            "name": "진동",
                            "value": f"{row[6]:.2f} g" if row[6] is not None else "0 g",
                            "rawValue": row[6] if row[6] is not None else 0,
                            "percentage": int(row[6]) if row[6] is not None else 0,
                            "status": "정상"
                        }
                    ]
                }
            else:
                # 기본값 반환
                return {
                    "currentFarm": 1,
                    "powerOn": False,
                    "connected": False,
                    "lastUpdate": datetime.now().isoformat(),
                    "sensors": []
                }
        finally:
            conn.close()
    
    def get_sensor_data_history(self, days: int = 7, farm_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """센서 데이터 히스토리 가져오기"""
        if not self.use_db:
            # 메모리 모드: 리스트에서 필터링
            if not self.sensor_data_list:
                return []
            
            result = []
            start_date = datetime.now() - timedelta(days=days)
            
            for data in self.sensor_data_list:
                try:
                    # 타임스탬프 파싱
                    if isinstance(data['timestamp'], str):
                        data_time = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
                    else:
                        continue
                    
                    # 날짜 필터링
                    if data_time >= start_date:
                        if farm_id is None or data['farm_id'] == farm_id:
                            result.append({
                                "id": data['id'],
                                "timestamp": data['timestamp'],
                                "farm_id": data['farm_id'],
                                "humidity": data['humidity'],
                                "temperature": data['temperature'],
                                "light": data['light'],
                                "soil_moisture": data['soil_moisture'],
                                "power_on": bool(data['power_on']),
                                "connected": bool(data['connected'])
                            })
                except:
                    continue
            
            return sorted(result, key=lambda x: x['timestamp'])
        
        # DB 모드
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            start_date = (datetime.now() - timedelta(days=days)).isoformat()
            
            if farm_id:
                cursor.execute('''
                    SELECT * FROM sensor_data 
                    WHERE timestamp >= ? AND farm_id = ?
                    ORDER BY timestamp ASC
                ''', (start_date, farm_id))
            else:
                cursor.execute('''
                    SELECT * FROM sensor_data 
                    WHERE timestamp >= ?
                    ORDER BY timestamp ASC
                ''', (start_date,))
            
            rows = cursor.fetchall()
            
            result = []
            for row in rows:
                result.append({
                    "id": row[0],
                    "timestamp": row[1],
                    "farm_id": row[2],
                    "humidity": row[3],
                    "temperature": row[4],
                    "light": row[5],
                    "soil_moisture": row[6],
                    "power_on": bool(row[7]),
                    "connected": bool(row[8])
                })
            
            return result
        finally:
            conn.close()
    
    def get_logs(self, limit: int = 500) -> List[Dict[str, Any]]:
        """로그 가져오기"""
        if not self.use_db:
            # 메모리 모드: 리스트에서 가져오기
            result = []
            for log in self.logs_list[-limit:]:
                result.append({
                    "timestamp": log.get('timestamp', datetime.now().strftime("%H:%M:%S")),
                    "message": log.get('message', ''),
                    "date": log.get('date', datetime.now().strftime("%Y-%m-%d")),
                    "log_type": log.get('log_type', 'info')
                })
            return result[::-1]  # 최신 순으로 반환
        
        # DB 모드
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            cursor.execute('''
                SELECT timestamp, message, date, log_type FROM logs 
                ORDER BY timestamp DESC 
                LIMIT ?
            ''', (limit,))
            
            rows = cursor.fetchall()
            
            result = []
            for row in rows:
                result.append({
                    "timestamp": row[0],
                    "message": row[1],
                    "date": row[2] or datetime.now().strftime("%Y-%m-%d"),
                    "log_type": row[3]
                })
            
            return result
        finally:
            conn.close()
    
    def add_log(self, message: str, log_type: str = 'info'):
        """로그 추가"""
        if not self.use_db:
            # 메모리 모드: 리스트에 추가
            log_entry = {
                'timestamp': datetime.now().strftime("%H:%M:%S"),
                'message': message,
                'date': datetime.now().strftime("%Y-%m-%d"),
                'log_type': log_type
            }
            self.logs_list.append(log_entry)
            
            # 최대 1000개까지만 유지
            if len(self.logs_list) > 1000:
                self.logs_list.pop(0)
            return
        
        # DB 모드
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            timestamp = datetime.now().strftime("%H:%M:%S")
            date = datetime.now().strftime("%Y-%m-%d")
            
            cursor.execute('''
                INSERT INTO logs (timestamp, message, date, log_type)
                VALUES (?, ?, ?, ?)
            ''', (timestamp, message, date, log_type))
            
            conn.commit()
        finally:
            conn.close()
    
    def save_logs_to_file(self, file_path: str, logs: List[str]) -> bool:
        """로그를 파일로 저장"""
        try:
            os.makedirs(os.path.dirname(file_path) if os.path.dirname(file_path) else '.', exist_ok=True)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                for log in logs:
                    f.write(log + '\n')
            
            return True
        except Exception as e:
            print(f"로그 파일 저장 오류: {e}")
            return False
    
    def load_logs_from_file(self, file_path: str) -> List[Dict[str, Any]]:
        """파일에서 로그 불러오기"""
        try:
            if not os.path.exists(file_path):
                return []
            
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            logs = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                # [HH:MM:SS] 형식의 타임스탬프 추출
                if line.startswith('[') and len(line) > 11:
                    timestamp = line[1:9] if ']' in line[:10] else datetime.now().strftime("%H:%M:%S")
                    message = line[11:].strip() if ']' in line[:10] else line
                else:
                    timestamp = datetime.now().strftime("%H:%M:%S")
                    message = line
                
                logs.append({
                    "timestamp": timestamp,
                    "message": message,
                    "date": datetime.now().strftime("%Y-%m-%d")
                })
            
            return logs[:500]  # 최대 500개
        except Exception as e:
            print(f"로그 파일 불러오기 오류: {e}")
            return []
    
    def parse_logs_from_content(self, content: str) -> List[Dict[str, Any]]:
        """로그 내용 파싱 - 다양한 타임스탬프 형식 지원"""
        import re
        try:
            lines = content.split('\n')
            logs = []
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                
                timestamp = datetime.now().strftime("%H:%M:%S")
                message = line
                
                # 다양한 타임스탬프 형식 지원
                if line.startswith('['):
                    # 형식 1: [HH:MM:SS] (예: [18:12:12])
                    match1 = re.match(r'\[(\d{2}:\d{2}:\d{2})\]\s*(.*)', line)
                    if match1:
                        timestamp = match1.group(1)
                        message = match1.group(2).strip()
                    else:
                        # 형식 2: [2025. 11. 19. 오후 5:19:41] 또는 [2025. 11. 19. 오전 10:30:15]
                        match2 = re.match(r'\[(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(오전|오후)\s*(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)', line)
                        if match2:
                            year = int(match2.group(1))
                            month = int(match2.group(2))
                            day = int(match2.group(3))
                            am_pm = match2.group(4)
                            hour = int(match2.group(5))
                            minute = int(match2.group(6))
                            second = int(match2.group(7))
                            message = match2.group(8).strip()
                            
                            # 오전/오후 변환
                            if am_pm == '오후' and hour != 12:
                                hour += 12
                            elif am_pm == '오전' and hour == 12:
                                hour = 0
                            
                            timestamp = f"{hour:02d}:{minute:02d}:{second:02d}"
                        else:
                            # 다른 형식의 타임스탬프 시도 (']' 앞부분을 타임스탬프로 간주)
                            bracket_end = line.find(']')
                            if bracket_end > 0:
                                timestamp_part = line[1:bracket_end]
                                message = line[bracket_end+1:].strip()
                                # 가능하면 타임스탬프 추출 시도
                                time_match = re.search(r'(\d{2}):(\d{2}):(\d{2})', timestamp_part)
                                if time_match:
                                    timestamp = time_match.group(0)
                
                logs.append({
                    "timestamp": timestamp,
                    "message": message,
                    "date": datetime.now().strftime("%Y-%m-%d")
                })
            
            return logs[:500]  # 최대 500개
        except Exception as e:
            print(f"로그 내용 파싱 오류: {e}")
            return []
    
    def extract_sensor_data_from_logs(self, logs: List[Dict[str, Any]], farm_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        로그에서 센서 데이터 추출하여 AI 분석용 형식으로 변환 (농장별 필터링 지원)
        
        Args:
            logs: 로그 리스트
            farm_id: 특정 농장 ID (None이면 모든 농장 데이터 추출, 로그에 farm_id 정보가 없으면 무시)
        """
        import re
        from datetime import datetime, timedelta
        
        sensor_data_list = []
        base_date = datetime.now().date()
        
        for log in logs:
            message = log.get('message', '')
            timestamp_str = log.get('timestamp', '')
            
            # 타임스탬프 파싱
            try:
                if timestamp_str and ':' in timestamp_str:
                    time_parts = timestamp_str.split(':')
                    if len(time_parts) >= 2:
                        hour = int(time_parts[0])
                        minute = int(time_parts[1])
                        second = int(time_parts[2]) if len(time_parts) > 2 else 0
                        timestamp = datetime.combine(base_date, datetime.min.time().replace(hour=hour, minute=minute, second=second))
                    else:
                        timestamp = datetime.now()
                else:
                    timestamp = datetime.now()
            except:
                timestamp = datetime.now()
            
            # 로그에서 farm_id 추출 (스마트팜 X번 형태로 저장된 경우)
            log_farm_id = None
            farm_match = re.search(r'스마트팜\s*(\d+)', message)
            if farm_match:
                log_farm_id = int(farm_match.group(1))
            elif 'farm_id' in log:
                log_farm_id = log.get('farm_id')
            
            # farm_id 필터링: 특정 농장 ID가 지정되었고, 로그에 farm_id가 있으면 필터링
            if farm_id is not None and log_farm_id is not None:
                if log_farm_id != farm_id:
                    continue  # 다른 농장 데이터는 스킵
            
            # 센서 데이터 추출
            sensor_data = {
                'timestamp': timestamp.isoformat(),
                'farm_id': log_farm_id if log_farm_id else (farm_id if farm_id else 1),  # 로그의 farm_id 우선 사용
                'humidity': None,
                'temperature': None,
                'light': None,
                'soil_moisture': None,
                'power_on': 1,
                'connected': 1
            }
            
            # 형식 1: 웹 표준 형식 우선 파싱: "센서데이터 습도:55.2% 온도:22.1℃ 채광:65.3% 토양습도:48.5%"
            if '센서데이터' in message:
                # 새 형식: 습도:값% 온도:값℃ 채광:값% 토양습도:값% (4개 센서 모두 포함되어야 함)
                humidity_match = re.search(r'습도:(\d+\.?\d*)\s*%', message)
                if humidity_match:
                    sensor_data['humidity'] = float(humidity_match.group(1))
                
                temp_match = re.search(r'온도:(\d+\.?\d*)\s*[℃°C]', message)
                if temp_match:
                    sensor_data['temperature'] = float(temp_match.group(1))
                
                light_match = re.search(r'채광:(\d+\.?\d*)\s*%', message)
                if light_match:
                    sensor_data['light'] = float(light_match.group(1))
                
                # 토양습도: 공백 포함/미포함 모두 지원 (토양습도: 또는 토양 습도:)
                soil_match = re.search(r'토양\s*습도:(\d+\.?\d*)\s*%', message)
                if soil_match:
                    sensor_data['soil_moisture'] = float(soil_match.group(1))
                
                # 새 형식은 4개 센서가 모두 있어야 완전한 데이터로 간주
                # 하나라도 없으면 해당 로그는 스킵 (다음 else 블록의 기존 형식도 시도하지 않음)
                if not all([sensor_data['humidity'] is not None,
                           sensor_data['temperature'] is not None,
                           sensor_data['light'] is not None,
                           sensor_data['soil_moisture'] is not None]):
                    continue  # 다음 로그로 이동
            # 형식 2: 실시간 데이터 저장 형식 (공백 없이 연결된 형식): "온도: 21.8°C습도: 26.7%채광: 1.3%토양 습도: 0.1%"
            elif '온도:' in message and '습도:' in message and ('채광:' in message or '토양' in message):
                # 실시간 데이터 형식: 온도:값°C습도:값%채광:값%토양 습도:값%
                # 온도 추출 (°C 또는 ℃ 모두 지원)
                temp_match = re.search(r'온도:\s*(\d+\.?\d*)\s*[℃°C]', message)
                if temp_match:
                    sensor_data['temperature'] = float(temp_match.group(1))
                
                # 습도 추출
                humidity_match = re.search(r'습도:\s*(\d+\.?\d*)\s*%', message)
                if humidity_match:
                    sensor_data['humidity'] = float(humidity_match.group(1))
                
                # 채광 추출
                light_match = re.search(r'채광:\s*(\d+\.?\d*)\s*%', message)
                if light_match:
                    sensor_data['light'] = float(light_match.group(1))
                
                # 토양 습도 추출 (공백 포함/미포함 모두 지원)
                soil_match = re.search(r'토양\s*습도:\s*(\d+\.?\d*)\s*%', message)
                if soil_match:
                    sensor_data['soil_moisture'] = float(soil_match.group(1))
                
                # 4개 센서가 모두 있어야 완전한 데이터로 간주
                if not all([sensor_data['humidity'] is not None,
                           sensor_data['temperature'] is not None,
                           sensor_data['light'] is not None,
                           sensor_data['soil_moisture'] is not None]):
                    continue  # 다음 로그로 이동
            else:
                # 기존 형식 지원 (하위 호환성): "습도 값 낮음 (45.5%)", "습도 정상 복귀 (낮음 → 정상, 현재값: 55.2%)"
                humidity_match = re.search(r'습도[^()]*\([^)]*?(\d+\.?\d*)\s*%', message)
                if humidity_match:
                    sensor_data['humidity'] = float(humidity_match.group(1))
                
                temp_match = re.search(r'온도[^()]*?\([^)]*?(\d+\.?\d*)\s*℃', message)
                if temp_match:
                    sensor_data['temperature'] = float(temp_match.group(1))
                
                light_match = re.search(r'채광[^()]*?\([^)]*?(\d+\.?\d*)\s*%', message)
                if light_match:
                    sensor_data['light'] = float(light_match.group(1))
                
                soil_match = re.search(r'토양습도[^()]*?\([^)]*?(\d+\.?\d*)\s*%', message)
                if soil_match:
                    sensor_data['soil_moisture'] = float(soil_match.group(1))
            
            # 센서 데이터가 하나라도 있으면 추가
            if any([sensor_data['humidity'] is not None, sensor_data['temperature'] is not None, 
                   sensor_data['light'] is not None, sensor_data['soil_moisture'] is not None]):
                sensor_data_list.append(sensor_data)
        
        return sensor_data_list
    
    def add_logs_from_json(self, logs_json: str) -> bool:
        """JSON 형식의 로그 추가"""
        try:
            logs = json.loads(logs_json)
            
            if not self.use_db:
                # 메모리 모드: 리스트에 추가
                for log in logs:
                    log_entry = {
                        'timestamp': log.get('timestamp', datetime.now().strftime("%H:%M:%S")),
                        'message': log.get('message', ''),
                        'date': log.get('date', datetime.now().strftime("%Y-%m-%d")),
                        'log_type': log.get('log_type', 'info')
                    }
                    self.logs_list.append(log_entry)
                
                # 최대 1000개까지만 유지
                if len(self.logs_list) > 1000:
                    self.logs_list = self.logs_list[-1000:]
                
                return True
            
            # DB 모드
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            for log in logs:
                timestamp = log.get('timestamp', datetime.now().strftime("%H:%M:%S"))
                message = log.get('message', '')
                date = log.get('date', datetime.now().strftime("%Y-%m-%d"))
                log_type = log.get('log_type', 'info')
                
                cursor.execute('''
                    INSERT INTO logs (timestamp, message, date, log_type)
                    VALUES (?, ?, ?, ?)
                ''', (timestamp, message, date, log_type))
            
            conn.commit()
            conn.close()
            
            return True
        except Exception as e:
            print(f"JSON 로그 추가 오류: {e}")
            return False

