import os
from datetime import datetime, timedelta, timezone

import redis
from flask import Flask, jsonify, request

app = Flask(__name__)
r = redis.Redis(host=os.environ.get("REDIS_HOST", "redis"), port=6379, decode_responses=True)
HOLD_SECONDS = int(os.environ.get("HOLD_SECONDS", "300"))
MAX_SEATS = int(os.environ.get("MAX_SEATS", "4"))  # 1인당 한 경기에서 동시에 선택할 수 있는 좌석 수
KST = timezone(timedelta(hours=9))

EVENTS = {
    1: {"id": 1, "name": "챔피언스 매치 블루 vs 레드", "category": "스포츠",
        "date": "2026-12-05 (토) 18:30", "venue": "티켓온 스타디움"},
    2: {"id": 2, "name": "올스타 페스티벌 드림 vs 나눔", "category": "스포츠",
        "date": "2026-12-12 (토) 14:00", "venue": "티켓온 아레나"},
}

# 행(row)별 등급: (등급 이름, 가격, 화면 색상 키)
GRADES = {
    "A": ("VIP석", 50000, "vip"),
    "B": ("VIP석", 50000, "vip"),
    "C": ("R석", 35000, "r"),
    "D": ("R석", 35000, "r"),
    "E": ("일반석", 20000, "s"),
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


def count_holds(event_id, nickname):
    """이 경기에서 닉네임이 현재 확보 중인 좌석 수"""
    holders = r.mget([f"hold:{event_id}:{s}" for s in SEATS])
    return sum(1 for h in holders if h == nickname)


@app.get("/api/health")
def health():
    r.ping()
    return jsonify(status="ok")


@app.get("/api/events")
def list_events():
    prices = [g[1] for g in GRADES.values()]
    events = [{**e, "min_price": min(prices), "max_price": max(prices)} for e in EVENTS.values()]
    return jsonify(events=events, max_seats=MAX_SEATS)


@app.get("/api/events/<int:event_id>/seats")
def get_seats(event_id):
    err = check(event_id)
    if err:
        return err
    nickname = request.args.get("nickname", "").strip()
    booked = r.smembers(f"booked:{event_id}")
    seats = []
    for seat in SEATS:
        grade, price, grade_key = GRADES[seat[0]]
        item = {"seat": seat, "grade": grade, "price": price, "grade_key": grade_key}
        if seat in booked:
            item["status"] = "booked"
        else:
            hold_key = f"hold:{event_id}:{seat}"
            ttl = r.ttl(hold_key)
            if ttl > 0:
                holder = r.get(hold_key)
                # 요청한 닉네임이 확보한 좌석이면 "mine", 다른 사람이면 "held"
                item["status"] = "mine" if nickname and holder == nickname else "held"
                item["remaining"] = ttl
            else:
                item["status"] = "available"
        seats.append(item)
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
    if count_holds(event_id, nickname) >= MAX_SEATS:
        return error(f"한 번에 최대 {MAX_SEATS}매까지 선택할 수 있습니다", 400)
    hold_key = f"hold:{event_id}:{seat}"
    if not r.set(hold_key, nickname, nx=True, ex=HOLD_SECONDS):
        return error("이미 선택된 좌석입니다", 409)
    # 확인과 저장 사이에 예매가 끝난 경우를 한 번 더 막는다
    if r.sismember(f"booked:{event_id}", seat):
        r.delete(hold_key)
        return error("이미 예매된 좌석입니다", 409)
    return jsonify(seat=seat, nickname=nickname, expires_in=HOLD_SECONDS), 201


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


@app.post("/api/events/<int:event_id>/confirm")
def confirm_seats(event_id):
    """선택한 좌석 여러 개를 한 번에 결제(예매 확정)한다. 하나라도 문제가 있으면 전체 실패."""
    err = check(event_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    nickname = str(data.get("nickname", "")).strip()
    seats = data.get("seats")
    if not nickname:
        return error("닉네임을 입력하세요", 400)
    if not isinstance(seats, list) or not seats:
        return error("결제할 좌석을 선택하세요", 400)
    seats = list(dict.fromkeys(str(s) for s in seats))  # 중복 제거
    if len(seats) > MAX_SEATS:
        return error(f"한 번에 최대 {MAX_SEATS}매까지 결제할 수 있습니다", 400)
    for s in seats:
        if s not in SEATS:
            return error("존재하지 않는 좌석입니다", 404)

    holders = r.mget([f"hold:{event_id}:{s}" for s in seats])
    expired = [s for s, h in zip(seats, holders) if h is None]
    if expired:
        return error("확보 시간이 만료되었습니다 (" + ", ".join(expired) + ")", 410)
    others = [s for s, h in zip(seats, holders) if h != nickname]
    if others:
        return error("다른 사람이 선택한 좌석입니다 (" + ", ".join(others) + ")", 403)

    booking_no = f"T{r.incr('booking_seq'):06d}"
    booked_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    items, total = [], 0
    for s in seats:
        grade, price, _ = GRADES[s[0]]
        r.hset(f"booking:{event_id}:{s}", mapping={
            "no": booking_no, "nickname": nickname, "booked_at": booked_at,
        })
        r.sadd(f"booked:{event_id}", s)
        r.sadd(f"user:{nickname}", f"{event_id}:{s}")
        r.delete(f"hold:{event_id}:{s}")
        items.append({"seat": s, "grade": grade, "price": price})
        total += price
    return jsonify(booking_no=booking_no, event=EVENTS[event_id]["name"],
                   seats=items, total=total, booked_at=booked_at)


@app.get("/api/bookings")
def my_bookings():
    nickname = request.args.get("nickname", "").strip()
    if not nickname:
        return error("닉네임을 입력하세요", 400)
    items = []
    for key in r.smembers(f"user:{nickname}"):
        event_id_text, seat = key.split(":")
        event_id = int(event_id_text)
        info = r.hgetall(f"booking:{event_id}:{seat}")
        if not info or event_id not in EVENTS:
            continue
        grade, price, _ = GRADES[seat[0]]
        items.append({
            "booking_no": info["no"], "event_id": event_id,
            "event": EVENTS[event_id]["name"], "date": EVENTS[event_id]["date"],
            "venue": EVENTS[event_id]["venue"], "seat": seat,
            "grade": grade, "price": price, "booked_at": info["booked_at"],
        })
    items.sort(key=lambda x: (x["booked_at"], x["booking_no"], x["seat"]), reverse=True)
    return jsonify(bookings=items)


@app.delete("/api/events/<int:event_id>/seats/<seat>/booking")
def cancel_booking(event_id, seat):
    err = check(event_id, seat)
    if err:
        return err
    nickname = get_nickname()
    if not nickname:
        return error("닉네임을 입력하세요", 400)
    key = f"booking:{event_id}:{seat}"
    info = r.hgetall(key)
    if not info:
        return error("예매 내역이 없습니다", 404)
    if info.get("nickname") != nickname:
        return error("본인의 예매만 취소할 수 있습니다", 403)
    r.srem(f"booked:{event_id}", seat)
    r.srem(f"user:{nickname}", f"{event_id}:{seat}")
    r.delete(key)
    return jsonify(seat=seat, status="available")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)