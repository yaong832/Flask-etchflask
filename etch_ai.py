# 식각 HMI AI — ML 모델(models/etch) 우선, 없으면 규칙 스텁.

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict

MODELS_DIR = os.path.join(os.path.dirname(__file__), "models", "etch")


def etch_ai_status_payload() -> Dict[str, Any]:
    try:
        from etch_model import get_engine

        return get_engine().status_payload()
    except Exception as ex:
        return {
            "project": "etch_hmi",
            "models_dir": MODELS_DIR,
            "ready": False,
            "engine": "stub",
            "message": f"AI 엔진 초기화 실패: {ex}",
        }


def _predict_alarm_stub(payload: dict) -> tuple[str, float]:
    """AI-2: 규칙 기반 예상 알람 (조언만, 인터락 비개입)."""
    alarm = payload.get("alarmCode") or payload.get("alarm_code")
    if alarm:
        code = str(alarm).strip().upper()
        return code if code.startswith("A") else "A000", 0.92

    state = (payload.get("equipmentState") or payload.get("equipment_state") or "").upper()
    if state == "ALARM":
        return "A001", 0.75

    interlock = payload.get("interlockOk")
    if interlock is False:
        return "A004", 0.62

    pressure = payload.get("pressure")
    if pressure is not None:
        try:
            p = float(pressure)
            lo = float(payload.get("pressureMin") or 50)
            hi = float(payload.get("pressureMax") or 150)
            if p < lo or p > hi:
                return "A002", 0.68
        except (TypeError, ValueError):
            pass

    vibration = payload.get("vibration")
    if vibration is not None:
        try:
            v = float(vibration)
            vmax = float(payload.get("vibrationMax") or 0.8)
            if v > vmax:
                return "A003", 0.65
        except (TypeError, ValueError):
            pass

    if state == "WARNING":
        return "A005", 0.45

    return "NONE", 0.18


def etch_ai_predict_stub(payload: dict) -> Dict[str, Any]:
    """규칙 기반 폴백."""
    alarm = payload.get("alarmCode") or payload.get("alarm_code")
    state = (payload.get("equipmentState") or payload.get("equipment_state") or "").upper()
    predicted_alarm, pred_confidence = _predict_alarm_stub(payload)
    score = 0.15
    if alarm:
        score = 0.85
    elif predicted_alarm not in (None, "", "NONE"):
        score = max(score, min(0.9, pred_confidence + 0.1))
    elif state == "WARNING":
        score = 0.55
    elif state == "ALARM":
        score = 0.92

    suggested = "정상 범위 모니터링을 유지하세요."
    if alarm:
        suggested = f"알람 {alarm} 원인 제거 후 Reset. 인터락·Load Lock 접촉을 확인하세요."
    elif predicted_alarm and predicted_alarm != "NONE":
        suggested = f"예상 알람 {predicted_alarm} — 추세·인터락·Load Lock을 점검하세요 (조언만)."
    elif state == "ALARM":
        suggested = "알람 상태 — 센서·접촉·EtherCAT 연결을 점검하세요."
    elif state == "WARNING":
        suggested = "환경(온·습도) 편차 — 공정 유지 시 추세를 강화 모니터링하세요."
    elif score >= 0.55:
        suggested = "이상 징후 가능 — 압력·진동 추세를 확인하세요."

    return {
        "success": True,
        "stub": True,
        "model": "rules",
        "anomaly_score": round(score, 3),
        "predicted_alarm": predicted_alarm,
        "prediction_confidence": round(pred_confidence, 3),
        "suggested_action": suggested,
        "note": "규칙 스텁 — models/etch/*.joblib 학습 후 ML로 전환됩니다.",
        "updatedAt": datetime.now().isoformat(),
    }


def etch_ai_predict(payload: dict) -> Dict[str, Any]:
    """ML 우선, 실패 시 규칙 스텁."""
    try:
        from etch_model import predict_ml

        ml = predict_ml(payload)
        if ml:
            ml["updatedAt"] = datetime.now().isoformat()
            return ml
    except Exception:
        pass
    return etch_ai_predict_stub(payload)
