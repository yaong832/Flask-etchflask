# 식각 HMI 웹·API 공통 기본값 (WPF appsettings.json 과 맞추면 됨)
import os
import socket

PRESSURE_UNIT = 'mTorr'
PRESSURE_INTERLOCK_MIN = 50.0
PRESSURE_INTERLOCK_MAX = 150.0
PRESSURE_GAUGE_MAX = 1000.0
PRESSURE_DECIMALS = 1

# Flask는 현장 PC에서 0.0.0.0 으로 띄우고, 모니터링 PC는 http://<현장PC_IP>:포트 로만 접속
FLASK_BIND_HOST = os.environ.get('ETCH_FLASK_HOST', '0.0.0.0')
FLASK_PORT = int(os.environ.get('ETCH_FLASK_PORT', '5000'))
# 1/true/yes 일 때만 디버그 모드 (기본 끔)
FLASK_DEBUG = os.environ.get('ETCH_FLASK_DEBUG', '').strip().lower() in ('1', 'true', 'yes')


def monitoring_api_meta() -> dict:
    """원격 모니터링(별도 PC) 구성 안내 — API/대시보드용."""
    try:
        host_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        host_ip = None
    return {
        'architecture': 'field_pc_wpf_ethercat + monitoring_pc_browser',
        'fieldPc': {
            'wpf': '현장 조작·안전 HMI',
            'ethercat': 'TwinCAT ADS · EtherCAT I/O',
            'flaskServer': 'WPF가 POST로 텔레메트리 전송 (같은 PC)',
        },
        'monitoringPc': {
            'role': '조회 전용',
            'access': f'http://<현장PC_IP>:{FLASK_PORT}/',
            'note': 'Flask·WPF·EtherCAT 마스터를 모니터링 PC에 설치할 필요 없음',
        },
        'serverBind': f'{FLASK_BIND_HOST}:{FLASK_PORT}',
        'serverLanIpHint': host_ip,
    }
