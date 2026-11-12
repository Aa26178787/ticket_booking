import os
import redis
from flask import Flask, jsonify, request

app = Flask(__name__)
r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379, decode_responses=True)
HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "300"))

EVENTS = {
    1: {"id": 1, "name": "경기 A (홈)"},
    2: {"id": 2, "name": "경기 B (원정)"},
}
SEATS = [f"{row}{col}" for row in "ABCDE" for col in range(1, 9)]  # A1~E8, 40석


def error(message, status):
    return jsonify(error=message), status


def check(event_id, seat=None):
    if event_id not in EVENTS:
        return error("경기를 찾을 수 없습니다", 404)
    if seat is not None and seat not in SEATS:
        return error("존재하지 않는 좌석입니다", 404)
    return None


def get_nickname():
    data = request.get_json(silent=True) or {}
    return str(data.get("nickname", "")).strip()


@app.get("/api/health")
def health():
    r.ping()
    return jsonify(status="ok")


@app.get("/api/events")
def list_events():
    return jsonify(events=list(EVENTS.values()))


@app.get("/api/events/<int:event_id>/seats")
def get_seats(event_id):
    err = check(event_id)
    if err:
        return err
    booked = r.smembers(f"booked:{event_id}")
    seats = []
    for seat in SEATS:
        if seat in booked:
            seats.append({"seat": seat, "status": "booked"})
            continue
        ttl = r.ttl(f"hold:{event_id}:{seat}")
        if ttl > 0:
            seats.append({"seat": seat, "status": "held", "remaining": ttl})
        else:
            seats.append({"seat": seat, "status": "available"})
    return jsonify(seats=seats)


@app.post("/api/events/<int:event_id>/seats/<seat>/hold")
def hold_seat(event_id, seat):
    err = check(event_id, seat)
    if err:
        return err
    nickname = get_nickname()
    if not nickname:
        return error("닉네임을 입력하세요", 400)
    if r.sismember(f"booked:{event_id}", seat):
        return error("이미 예매된 좌석입니다", 409)
    hold_key = f"hold:{event_id}:{seat}"
    if not r.set(hold_key, nickname, nx=True, ex=HOLD_SECONDS):
        return error("이미 선택된 좌석입니다", 409)
    # 확인과 저장 사이에 예매가 끝난 경우를 한 번 더 막는다
    if r.sismember(f"booked:{event_id}", seat):
        r.delete(hold_key)
        return error("이미 예매된 좌석입니다", 409)
    return jsonify(seat=seat, nickname=nickname, expires_in=HOLD_SECONDS), 201


@app.post("/api/events/<int:event_id>/seats/<seat>/confirm")
def confirm_seat(event_id, seat):
    err = check(event_id, seat)
    if err:
        return err
    nickname = get_nickname()
    hold_key = f"hold:{event_id}:{seat}"
    holder = r.get(hold_key)
    if holder is None:
        return error("확보 시간이 만료되었습니다", 410)
    if holder != nickname:
        return error("다른 사람이 선택한 좌석입니다", 403)
    r.sadd(f"booked:{event_id}", seat)
    r.delete(hold_key)
    return jsonify(seat=seat, status="booked")


@app.delete("/api/events/<int:event_id>/seats/<seat>/hold")
def cancel_hold(event_id, seat):
    err = check(event_id, seat)
    if err:
        return err
    nickname = get_nickname()
    hold_key = f"hold:{event_id}:{seat}"
    holder = r.get(hold_key)
    if holder is None:
        return error("취소할 선택 내역이 없습니다", 404)
    if holder != nickname:
        return error("본인이 선택한 좌석만 취소할 수 있습니다", 403)
    r.delete(hold_key)
    return jsonify(seat=seat, status="available")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)