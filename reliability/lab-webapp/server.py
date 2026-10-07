#!/usr/bin/env python3
"""Live Scoreboard & Static File Server for Operation Goodput CE Lab."""

import json
import os
import threading
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

WEBAPP_DIR = os.path.dirname(os.path.abspath(__file__))
SCOREBOARD_FILE = os.path.join(WEBAPP_DIR, "scoreboard.json")
INSTRUCTOR_PASSWORD = "ilovejensen"

_lock = threading.Lock()


def _load_db():
  if not os.path.exists(SCOREBOARD_FILE):
    return {"players": {}, "updatedAt": int(time.time() * 1000)}
  try:
    with open(SCOREBOARD_FILE, "r", encoding="utf-8") as f:
      data = json.load(f)
      if "players" not in data or not isinstance(data["players"], dict):
        data["players"] = {}
      return data
  except Exception:
    return {"players": {}, "updatedAt": int(time.time() * 1000)}


def _save_db(data):
  data["updatedAt"] = int(time.time() * 1000)
  tmp_path = SCOREBOARD_FILE + ".tmp"
  with open(tmp_path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2)
  os.replace(tmp_path, SCOREBOARD_FILE)


def _format_leaderboard(db):
  players_list = list(db.get("players", {}).values())
  # Sort by:
  # 1) totalScore DESC
  # 2) solvedCount DESC
  # 3) lastScoreAt ASC (who reached that score first)
  players_list.sort(
      key=lambda p: (
          -int(p.get("totalScore", 0)),
          -int(p.get("solvedCount", 0)),
          int(p.get("lastScoreAt", 9999999999999)),
      )
  )
  for idx, p in enumerate(players_list):
    p["rank"] = idx + 1
  return {
      "leaderboard": players_list,
      "updatedAt": db.get("updatedAt", int(time.time() * 1000)),
  }


class LabRequestHandler(SimpleHTTPRequestHandler):
  """Serves static assets and /api/* endpoints for the live scoreboard."""

  def __init__(self, *args, **kwargs):
    super().__init__(*args, directory=WEBAPP_DIR, **kwargs)

  def log_message(self, fmt, *args):
    # Keep logs concise
    if self.path.startswith("/api/scoreboard"):
      return
    super().log_message(fmt, *args)

  def _send_json(self, payload, status=HTTPStatus.OK):
    raw = json.dumps(payload).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  def _read_json_body(self):
    length = int(self.headers.get("Content-Length", "0"))
    if length <= 0:
      return {}
    raw = self.rfile.read(length)
    return json.loads(raw.decode("utf-8"))

  def do_OPTIONS(self):
    self.send_response(HTTPStatus.NO_CONTENT)
    self.send_header("Access-Control-Allow-Origin", "*")
    self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    self.send_header("Access-Control-Allow-Headers", "Content-Type")
    self.end_headers()

  def do_GET(self):
    parsed = urlparse(self.path)
    if parsed.path == "/api/scoreboard":
      with _lock:
        db = _load_db()
        resp = _format_leaderboard(db)
      self._send_json(resp)
      return
    super().do_GET()

  def do_POST(self):
    parsed = urlparse(self.path)
    try:
      body = self._read_json_body()
    except Exception as e:
      self._send_json({"error": f"Invalid JSON: {e}"}, HTTPStatus.BAD_REQUEST)
      return

    now_ms = int(time.time() * 1000)

    if parsed.path == "/api/register":
      player_id = str(body.get("playerId", "")).strip()
      nickname = str(body.get("nickname", "")).strip()[:28]
      if not player_id or not nickname:
        self._send_json(
            {"error": "playerId and nickname are required"},
            HTTPStatus.BAD_REQUEST,
        )
        return
      with _lock:
        db = _load_db()
        existing = db["players"].get(player_id, {})
        clue_scores = existing.get("clueScores", {})
        total_score = sum(
            int(v.get("points", 0)) for v in clue_scores.values()
        )
        solved_count = len(clue_scores)
        db["players"][player_id] = {
            "playerId": player_id,
            "nickname": nickname,
            "totalScore": total_score,
            "solvedCount": solved_count,
            "currentMissionIdx": int(
                body.get("currentMissionIdx", existing.get("currentMissionIdx", 0))
            ),
            "clueScores": clue_scores,
            "joinedAt": existing.get("joinedAt", now_ms),
            "lastScoreAt": existing.get("lastScoreAt", now_ms),
            "updatedAt": now_ms,
        }
        _save_db(db)
        resp = _format_leaderboard(db)
      self._send_json(resp)
      return

    if parsed.path == "/api/score":
      player_id = str(body.get("playerId", "")).strip()
      nickname = str(body.get("nickname", "")).strip()[:28] or "Anonymous CE"
      clue_id = str(body.get("clueId", "")).strip()
      points = max(0, min(1000, int(body.get("points", 0))))
      elapsed_sec = max(0, int(body.get("elapsedSec", 0)))
      revealed = bool(body.get("revealed", False))
      current_mission_idx = int(body.get("currentMissionIdx", 0))

      if not player_id or not clue_id:
        self._send_json(
            {"error": "playerId and clueId are required"},
            HTTPStatus.BAD_REQUEST,
        )
        return

      with _lock:
        db = _load_db()
        player = db["players"].get(
            player_id,
            {
                "playerId": player_id,
                "nickname": nickname,
                "totalScore": 0,
                "solvedCount": 0,
                "currentMissionIdx": current_mission_idx,
                "clueScores": {},
                "joinedAt": now_ms,
                "lastScoreAt": now_ms,
                "updatedAt": now_ms,
            },
        )
        player["nickname"] = nickname
        player["currentMissionIdx"] = max(
            int(player.get("currentMissionIdx", 0)), current_mission_idx
        )
        clue_scores = player.get("clueScores", {})
        # Only record score for a clue once (prevent re-submitting to farm points)
        if clue_id not in clue_scores:
          clue_scores[clue_id] = {
              "points": 0 if revealed else points,
              "elapsedSec": elapsed_sec,
              "revealed": revealed,
              "solvedAt": now_ms,
          }
          player["lastScoreAt"] = now_ms

        player["clueScores"] = clue_scores
        player["totalScore"] = sum(
            int(v.get("points", 0)) for v in clue_scores.values()
        )
        player["solvedCount"] = len(clue_scores)
        player["updatedAt"] = now_ms
        db["players"][player_id] = player
        _save_db(db)
        resp = _format_leaderboard(db)
      self._send_json(resp)
      return

    if parsed.path == "/api/reset-player":
      player_id = str(body.get("playerId", "")).strip()
      if not player_id:
        self._send_json(
            {"error": "playerId is required"}, HTTPStatus.BAD_REQUEST
        )
        return
      with _lock:
        db = _load_db()
        if player_id in db["players"]:
          nickname = db["players"][player_id].get("nickname", "Anonymous CE")
          db["players"][player_id] = {
              "playerId": player_id,
              "nickname": nickname,
              "totalScore": 0,
              "solvedCount": 0,
              "currentMissionIdx": 0,
              "clueScores": {},
              "joinedAt": now_ms,
              "lastScoreAt": now_ms,
              "updatedAt": now_ms,
          }
          _save_db(db)
        resp = _format_leaderboard(db)
      self._send_json(resp)
      return

    if parsed.path == "/api/reset-scoreboard":
      password = str(body.get("password", "")).strip()
      if password != INSTRUCTOR_PASSWORD:
        self._send_json(
            {"error": "Invalid instructor password"}, HTTPStatus.FORBIDDEN
        )
        return
      with _lock:
        db = {"players": {}, "updatedAt": now_ms}
        _save_db(db)
        resp = _format_leaderboard(db)
      self._send_json(resp)
      return

    self._send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)


def main():
  port = int(os.environ.get("PORT", "8787"))
  server = ThreadingHTTPServer(("0.0.0.0", port), LabRequestHandler)
  print(f"Operation Goodput CE Lab Server listening on http://0.0.0.0:{port}")
  server.serve_forever()


if __name__ == "__main__":
  main()
