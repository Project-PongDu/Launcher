#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py  —  퍼펫 API : 치지직 → 좀보이드 후원연동 (단일 창, 치지직 전용)

설치:  pip install chzzkpy PyQt5
실행:  python gui.py

구조 (양쪽 끝에 "돼지코(어댑터)"를 끼운 형태)
    DonationSource / ChzzkpySource : 치지직 수신 추상화. chzzkpy 의존은 ChzzkpySource 안에만 존재.
                                     -> 공식 API로 바꾸려면 이 클래스만 새로 구현하면 됨.
    GameAdapter / ZomboidAdapter   : 게임별 출력(경로 탐지 + rewards.txt 기록). 게임 확장 포인트.
    DonationWorker                 : 코어. 스레드+asyncio로 Source 를 돌리고 Qt 시그널로 GUI에 전달.
                                     -> chzzkpy 도 게임 파일도 직접 모름. 어댑터한테만 말 건다.
    MainWindow                     : PyQt5 단일 창 UI.

19세 방송:  네이버 NID 쿠키(성인인증 계정)를 넘기면 19+ 방송도 수신. 쿠키는 약 한 달이면 만료.
라인 포맷(모드 DonationReceiver.lua 규약):  amount,sender,message   (sender/message URL 인코딩)
"""

import asyncio
import json
import os
import re
import sys
import threading
import time
from collections import namedtuple
from pathlib import Path
from urllib.parse import quote

from PyQt5.QtCore import Qt, QObject, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QTextEdit, QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog, QFrame,
    QCheckBox,
)


# ── 로컬 설정 (홈 폴더, exe 옆 아님 -> 권한 문제 회피) ────────────────────────
CONFIG_DIR = Path.home() / ".chzzk_zomboid"
CONFIG_PATH = CONFIG_DIR / "config.json"

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_config(d: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── 채널 입력 정규화 ──────────────────────────────────────────────────────────
HEX32 = re.compile(r"[0-9a-fA-F]{32}")

def resource_path(rel):
    """exe(PyInstaller)로 묶였을 때든 그냥 실행이든 리소스 파일 경로를 찾는다."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

ICON_FILE = "PuppetAPI_smart.ico"

def extract_uuid(text: str):
    """입력 어디에 있든 32자리 hex(=채널 UUID)를 뽑는다. URL/라이브URL/생UUID 다 처리."""
    m = HEX32.search(text or "")
    return m.group(0).lower() if m else None


# ═══════════════════════════════════════════════════════════════════════════════
#  치지직 어댑터 (수신 방식 추상화 = "돼지코")
#  chzzkpy 의존은 ChzzkpySource 안에만 존재. 공식 API로 바꾸려면 이 클래스만 갈아끼우면 됨.
# ═══════════════════════════════════════════════════════════════════════════════
Donation = namedtuple("Donation", "amount sender message")   # 플랫폼 중립 도네 1건


class SourceError(Exception):
    pass

class AdultVerificationRequired(SourceError):   # 19+ 방송인데 쿠키 없음/만료
    pass

class ChannelOffline(SourceError):              # 방송 꺼져 있음
    pass


class DonationSource:
    """치지직 수신 인터페이스. chzzkpy든 공식 API든 이 3개만 구현하면 코어는 안 바뀐다."""

    async def resolve_channel(self, text):
        """입력(URL/채널명/UUID) -> (uuid, 표시이름). 못 찾으면 (None, 사유)."""
        raise NotImplementedError

    async def connect(self, uuid, emit, nid_aut=None, nid_ses=None):
        """연결 후 도네마다 emit(Donation) 호출. 정상 종료 시 리턴, 문제 시 SourceError."""
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError


class ChzzkpySource(DonationSource):
    """chzzkpy(비공식) 기반 구현. ← 이 파일에서 chzzkpy 를 import/호출하는 유일한 곳."""

    def __init__(self, grace_sec=3.0):
        self.grace = grace_sec
        self._client = None

    async def resolve_channel(self, text):
        uuid = extract_uuid(text)
        if uuid:
            return uuid, (await self._fetch_channel_name(uuid) or "")
        name = (text or "").strip()
        if not name:
            return None, "빈 입력"
        try:
            from chzzkpy.unofficial import Client
            c = Client()
            res = await c.search_channel(name)
            await c.close()
        except Exception:
            return None, "검색 실패"
        if not res:
            return None, "검색 결과 없음"
        return res[0].id, res[0].name

    async def _fetch_channel_name(self, uuid):
        """UUID로 직접 입력했을 때 채널명을 치지직 공개 API에서 가져온다. (chzzkpy 미지원이라 직접 호출)"""
        import aiohttp
        url = f"https://api.chzzk.naver.com/service/v1/channels/{uuid}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get(url) as r:
                    data = await r.json()
            return ((data or {}).get("content") or {}).get("channelName")
        except Exception:
            return None

    async def connect(self, uuid, emit, nid_aut=None, nid_ses=None):
        from chzzkpy.unofficial.chat import ChatClient
        self._client = ChatClient(uuid)
        started = time.monotonic()
        grace = self.grace

        @self._client.event
        async def on_donation(message):     # chzzkpy 가 함수명으로 이벤트 매칭 -> 이름 고정
            if time.monotonic() - started < grace:
                return                       # 접속 직후 리플레이된 과거 도네 무시
            ex = getattr(message, "extras", None)
            try:
                amt = int(getattr(ex, "pay_amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0
            if amt <= 0:
                return
            anon = bool(getattr(ex, "is_anonymous", False))
            prof = getattr(message, "profile", None)
            nick = getattr(prof, "nickname", None) if prof else None
            sender = "익명의 후원자" if (anon or not nick) else nick
            body = (getattr(message, "content", "") or "").replace("\r", " ").replace("\n", " ").strip()
            emit(Donation(amt, sender, body))   # ← 코어로는 chzzkpy 객체가 아니라 Donation 만 넘어감

        try:
            await self._client.start(nid_aut, nid_ses)
        except Exception as e:
            low = f"{type(e).__name__}: {e}".lower()
            if "adult" in low or "verification" in low:
                raise AdultVerificationRequired() from e
            if any(k in low for k in ("chat_channel", "channel_is_null", "is_null", "not live", "offline")):
                raise ChannelOffline() from e
            raise

    async def close(self):
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
#  게임 어댑터 (출력 대상 추상화 = "돼지코")
# ═══════════════════════════════════════════════════════════════════════════════
class GameAdapter:
    name = "game"
    TIERS: dict = {}

    def __init__(self):
        self.path = None  # Path | None

    def find_path(self):
        raise NotImplementedError

    def write(self, amount, sender, message):
        raise NotImplementedError


class ZomboidAdapter(GameAdapter):
    name = "좀보이드"
    TIERS = {
        1000: "디버프 룰렛", 2000: "버프 룰렛", 5000: "좀비 룰렛",
        10000: "스프린터 5마리", 20000: "밴딧(근접)", 35000: "백신",
        40000: "밴딧(원거리)", 50000: "추방 텔레포트", 100000: "백룸",
        150000: "미사일 폭격",
    }

    def find_path(self):
        home = Path.home()
        cands = [
            home / "Zomboid" / "Lua" / "rewards.txt",
            Path(os.environ.get("USERPROFILE", home)) / "Zomboid" / "Lua" / "rewards.txt",
        ]
        for env in ("OneDrive", "OneDriveConsumer"):
            od = os.environ.get(env)
            if od:
                cands.append(Path(od) / "Zomboid" / "Lua" / "rewards.txt")
        for c in cands:
            if c.parent.exists():
                return c
        for drive in ("C:", "D:", "E:", "F:"):
            base = Path(drive + "\\Users")
            if base.exists():
                try:
                    for user in base.iterdir():
                        p = user / "Zomboid" / "Lua"
                        if p.exists():
                            return p / "rewards.txt"
                except OSError:
                    pass
        return cands[0]

    @staticmethod
    def _enc(s):
        return quote(s or "", safe="")  # 모드 urldecode 가 %XX 만 풀어서 공백/콤마/줄바꿈/한글 전부 인코딩

    def write(self, amount, sender, message):
        line = "%d,%s,%s" % (int(amount), self._enc(sender), self._enc(message))
        if self.path is None:
            raise RuntimeError("rewards.txt 경로가 설정되지 않음")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        return line


# ═══════════════════════════════════════════════════════════════════════════════
#  코어: 수신 워커 (스레드 + asyncio -> Qt 시그널, 플랫폼/게임 중립)
# ═══════════════════════════════════════════════════════════════════════════════
class DonationWorker(QObject):
    donation = pyqtSignal(int, str, str)   # amount, sender, message
    status   = pyqtSignal(str, str)        # text, color(hex)
    resolved = pyqtSignal(str, str)        # uuid, display_name
    failed   = pyqtSignal(str)             # 멈춤
    note     = pyqtSignal(str)             # 로그용 (안 멈춤)

    def __init__(self, source, channel_text, nid_aut="", nid_ses="", reconnect_sec=5.0):
        super().__init__()
        self.source = source               # ← 어떤 수신 방식이든 DonationSource 만 받는다
        self.channel_text = channel_text
        self.nid_aut = nid_aut or None
        self.nid_ses = nid_ses or None
        self.reconnect = reconnect_sec
        self._stop = False
        self._thread = None
        self.loop = None
        self._last_note = None

    def _note_once(self, msg):
        if msg != self._last_note:
            self._last_note = msg
            self.note.emit(msg)

    def _emit(self, d):                    # Donation -> Qt 시그널
        self.donation.emit(d.amount, d.sender, d.message)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self.loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.source.close(), self.loop)
                fut.result(timeout=3)
            except Exception:
                pass

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        uuid, name = self.loop.run_until_complete(self.source.resolve_channel(self.channel_text))
        if not uuid:
            self.failed.emit(f"채널을 못 찾음 ({name}) — URL · 채널명 · UUID 확인해줘")
            self.status.emit("대기 중", "#5f5e5a")
            return
        self.resolved.emit(uuid, name or "")

        while not self._stop:
            try:
                self.status.emit("연결됨", "#5dcaa5")
                self.loop.run_until_complete(
                    self.source.connect(uuid, self._emit, self.nid_aut, self.nid_ses))
                if self._stop:
                    break
                self._note_once("연결 끊김 (방송 종료?) — 재접속 대기 중")
                self.status.emit("재접속 대기…", "#ef9f27")
            except AdultVerificationRequired:
                self.failed.emit("19세 방송이야. ‘19세 방송 연동’을 켜고 성인인증된 네이버 쿠키를 넣어줘. "
                                 "(이미 넣었다면 쿠키가 만료됐을 수 있음 — 갱신 필요)")
                return
            except ChannelOffline:
                if self._stop:
                    break
                self._note_once("방송이 꺼져 있어. 방송 시작하면 자동으로 연결됨.")
                self.status.emit("방송 대기 중", "#ef9f27")
            except Exception as e:
                if self._stop:
                    break
                self._note_once("연결 오류: " + f"{type(e).__name__}: {e}")
                self.status.emit("재접속 대기…", "#ef9f27")
            self._sleep(self.reconnect)

        self.status.emit("대기 중", "#5f5e5a")

    def _sleep(self, sec):
        end = time.monotonic() + sec
        while time.monotonic() < end and not self._stop:
            time.sleep(0.2)


# ── 메인 창 ───────────────────────────────────────────────────────────────────
DARK_QSS = """
QWidget { background:#23252b; color:#e8e8ea; font-family:'Malgun Gothic','맑은 고딕',sans-serif; font-size:13px; }
QLineEdit, QComboBox { background:#1b1d22; border:1px solid rgba(255,255,255,0.12); border-radius:8px; padding:7px 10px; color:#e8e8ea; }
QLineEdit:focus, QComboBox:focus { border:1px solid #1d9e75; }
QLineEdit:disabled { color:#5f5e5a; }
QTextEdit { background:#15171b; border:1px solid rgba(255,255,255,0.08); border-radius:8px; color:#b8bac0; font-family:Consolas,monospace; font-size:12px; }
QPushButton { background:#2b2e36; border:1px solid rgba(255,255,255,0.15); border-radius:8px; padding:7px 14px; color:#e8e8ea; }
QPushButton:hover { background:#343843; }
QPushButton#start { background:#1d9e75; color:#04342c; border:none; font-weight:bold; padding:10px 20px; }
QPushButton#start:hover { background:#22b384; }
QPushButton#stop  { background:#a32d2d; color:#ffe; border:none; font-weight:bold; padding:10px 20px; }
QPushButton#link  { background:transparent; border:none; color:#85b7eb; padding:2px; }
QCheckBox { color:#cfd0d4; font-size:12px; }
QLabel#muted { color:#9a9ca3; font-size:12px; }
QLabel#hint  { color:#6f7178; font-size:11px; }
QLabel#tier  { background:#2b2e36; border-radius:6px; padding:7px 10px; font-size:12px; color:#cfd0d4; }
QFrame#sep { background:rgba(255,255,255,0.08); max-height:1px; }
"""


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("퍼펫 API Launcher")
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.resize(620, 860)
        self.adapter = ZomboidAdapter()
        self.worker = None
        self.cfg = load_config()
        self._build()
        self._restore()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        title = QLabel("치지직 → 좀보이드 후원연동")
        title.setStyleSheet("font-size:15px; font-weight:bold;")
        root.addWidget(title)

        # 채널
        root.addWidget(self._muted("치지직 채널  —  URL · 채널명 · UUID 아무거나"))
        self.channel_input = QLineEdit()
        self.channel_input.setPlaceholderText("https://chzzk.naver.com/live/…  또는  채널명")
        root.addWidget(self.channel_input)
        self.channel_state = QLabel(" "); self.channel_state.setObjectName("muted")
        root.addWidget(self.channel_state)

        # 경로
        root.addWidget(self._muted("rewards.txt 경로"))
        prow = QHBoxLayout()
        self.path_input = QLineEdit(); self.path_input.setReadOnly(True)
        prow.addWidget(self.path_input, 1)
        redetect = QPushButton("다시 탐지"); redetect.setObjectName("link"); redetect.clicked.connect(self._autodetect_path)
        choose = QPushButton("직접 지정"); choose.setObjectName("link"); choose.clicked.connect(self._choose_path)
        prow.addWidget(redetect); prow.addWidget(choose)
        root.addLayout(prow)

        # 19세 방송 (쿠키)
        self.adult_check = QCheckBox("19세 방송 연동  (네이버 쿠키 필요)")
        self.adult_check.toggled.connect(self._on_adult_toggle)
        root.addWidget(self.adult_check)
        crow = QHBoxLayout()
        self.nid_aut_input = QLineEdit(); self.nid_aut_input.setPlaceholderText("NID_AUT")
        self.nid_ses_input = QLineEdit(); self.nid_ses_input.setPlaceholderText("NID_SES")
        for w in (self.nid_aut_input, self.nid_ses_input):
            w.setEchoMode(QLineEdit.Password); w.setEnabled(False)
        crow.addWidget(self.nid_aut_input); crow.addWidget(self.nid_ses_input)
        root.addLayout(crow)
        self.remember_cookies = QCheckBox("쿠키 기억"); self.remember_cookies.setEnabled(False)
        root.addWidget(self.remember_cookies)
        self.cookie_hint = QLabel("치지직 로그인 후 F12 → Application → Cookies 에서 복사 · 성인인증된 계정 · 약 한 달이면 만료")
        self.cookie_hint.setObjectName("hint"); self.cookie_hint.setWordWrap(True); self.cookie_hint.setVisible(False)
        root.addWidget(self.cookie_hint)

        # 시작/중지 + 상태
        srow = QHBoxLayout()
        self.start_btn = QPushButton("연동 시작"); self.start_btn.setObjectName("start")
        self.start_btn.clicked.connect(self._toggle)
        srow.addWidget(self.start_btn)
        self.status_dot = QLabel("●"); self.status_dot.setStyleSheet("color:#5f5e5a; font-size:14px;")
        self.status_text = QLabel("대기 중"); self.status_text.setObjectName("muted")
        srow.addWidget(self.status_dot); srow.addWidget(self.status_text); srow.addStretch(1)
        root.addLayout(srow)

        root.addWidget(self._sep())

        # 리워드 티어 (표시용)
        root.addWidget(self._muted("리워드 티어  —  정확히 일치하는 금액만 발동"))
        grid = QGridLayout(); grid.setSpacing(6)
        accent = {35000: "#85b7eb", 50000: "#f0997b", 100000: "#f0997b", 150000: "#e24b4a"}
        for i, (amt, label) in enumerate(self.adapter.TIERS.items()):
            col = accent.get(amt, "#5dcaa5")
            lbl = QLabel(f"<b style='color:{col}'>{amt:,}</b>&nbsp; {label}"); lbl.setObjectName("tier")
            grid.addWidget(lbl, i // 2, i % 2)
        root.addLayout(grid)

        root.addWidget(self._sep())

        # 테스트 후원
        trow = QHBoxLayout()
        trow.addWidget(self._muted("테스트 후원"))
        self.test_combo = QComboBox()
        for amt, label in self.adapter.TIERS.items():
            self.test_combo.addItem(f"{amt:,} — {label}", amt)
        trow.addWidget(self.test_combo, 1)
        inject = QPushButton("확인"); inject.clicked.connect(self._inject_test)
        trow.addWidget(inject)
        root.addLayout(trow)

        # 로그
        root.addWidget(self._muted("실시간 도네 로그"))
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMinimumHeight(140)
        root.addWidget(self.log, 1)

        self.setStyleSheet(DARK_QSS)

    # --- 헬퍼 ---
    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _sep(self):
        f = QFrame(); f.setObjectName("sep"); f.setFixedHeight(1); return f

    # --- 설정 복원/저장 ---
    def _restore(self):
        self.channel_input.setText(self.cfg.get("channel", ""))
        manual = self.cfg.get("path", "")
        if manual:
            self.adapter.path = Path(manual)
            self.path_input.setText(manual)
        else:
            self._autodetect_path()
        if self.cfg.get("adult", False):
            self.adult_check.setChecked(True)
        if self.cfg.get("remember_cookies", False):
            self.remember_cookies.setChecked(True)
            self.nid_aut_input.setText(self.cfg.get("nid_aut", ""))
            self.nid_ses_input.setText(self.cfg.get("nid_ses", ""))

    def _persist(self):
        remember = self.remember_cookies.isChecked()
        self.cfg.update({
            "channel": self.channel_input.text().strip(),
            "path": str(self.adapter.path) if self.adapter.path else "",
            "adult": self.adult_check.isChecked(),
            "remember_cookies": remember,
            "nid_aut": self.nid_aut_input.text() if remember else "",
            "nid_ses": self.nid_ses_input.text() if remember else "",
        })
        save_config(self.cfg)

    def closeEvent(self, e):
        self._persist()
        if self.worker:
            self.worker.stop()
        super().closeEvent(e)

    # --- 19세 토글 ---
    def _on_adult_toggle(self, on):
        for w in (self.nid_aut_input, self.nid_ses_input, self.remember_cookies):
            w.setEnabled(on)
        self.cookie_hint.setVisible(on)

    # --- 경로 ---
    def _autodetect_path(self):
        p = self.adapter.find_path()
        self.adapter.path = p
        if p:
            self.path_input.setText(str(p))
            exists = p.exists()
            self._log(f"경로 {'탐지' if exists else '예정'}: {p}" + ("" if exists else "  (첫 후원 때 생성됨)"))
        else:
            self.path_input.setText("")
            self._log("rewards.txt 경로를 못 찾음. ‘직접 지정’으로 골라줘.")

    def _choose_path(self):
        start_dir = str(self.adapter.path.parent) if self.adapter.path else str(Path.home())
        fn, _ = QFileDialog.getSaveFileName(self, "rewards.txt 위치 선택", start_dir, "Text (*.txt)")
        if fn:
            self.adapter.path = Path(fn)
            self.path_input.setText(fn)
            self._log(f"경로 수동 지정: {fn}")

    # --- 시작/중지 ---
    def _toggle(self):
        self._start() if self.worker is None else self._stop()

    def _start(self):
        ch = self.channel_input.text().strip()
        if not ch:
            self._log("채널을 먼저 입력해줘."); return
        if self.adapter.path is None:
            self._log("rewards.txt 경로가 없어. ‘직접 지정’으로 골라줘."); return
        nid_aut = self.nid_aut_input.text().strip() if self.adult_check.isChecked() else ""
        nid_ses = self.nid_ses_input.text().strip() if self.adult_check.isChecked() else ""
        self._persist()
        source = ChzzkpySource()                       # ← 수신 어댑터. 공식 API 가면 여기만 교체.
        self.worker = DonationWorker(source, ch, nid_aut, nid_ses)
        self.worker.donation.connect(self._on_donation)
        self.worker.status.connect(self._on_status)
        self.worker.resolved.connect(self._on_resolved)
        self.worker.failed.connect(self._on_failed)
        self.worker.note.connect(self._log)
        self.worker.start()
        self.start_btn.setText("중지"); self.start_btn.setObjectName("stop"); self.setStyleSheet(DARK_QSS)
        self.channel_input.setEnabled(False)
        self._log("연동 시작…" + ("  (19세 방송 모드)" if self.adult_check.isChecked() else ""))

    def _stop(self):
        if self.worker:
            self.worker.stop(); self.worker = None
        self.start_btn.setText("연동 시작"); self.start_btn.setObjectName("start"); self.setStyleSheet(DARK_QSS)
        self.channel_input.setEnabled(True)
        self._on_status("대기 중", "#5f5e5a")
        self._log("중지됨.")

    # --- 시그널 핸들러 ---
    def _on_donation(self, amount, sender, message):
        self.adapter.write(amount, sender, message)
        if amount in self.adapter.TIERS:
            self._log(f"{sender}  {amount:,}원  →  {self.adapter.TIERS[amount]}")
        else:
            self._log(f"{sender}  {amount:,}원  (통계만)")

    def _on_status(self, text, color):
        self.status_text.setText(text)
        self.status_dot.setStyleSheet(f"color:{color}; font-size:14px;")

    def _on_resolved(self, uuid, name):
        short = f"{uuid[:8]}…{uuid[-3:]}"
        label = name if name else short
        self.channel_state.setText(f"채널 인식됨 · {label}")
        self.channel_state.setStyleSheet("color:#5dcaa5; font-size:12px;")

    def _on_failed(self, msg):
        self._log("⚠ " + msg)
        self._stop()

    def _inject_test(self):
        amt = self.test_combo.currentData()
        if self.adapter.path is None:
            self._log("경로가 없어서 테스트 불가. 경로 먼저 지정해줘."); return
        self.adapter.write(amt, "테스트후원자", "테스트")
        self._log(f"[테스트] {amt:,}원 적용  →  {self.adapter.TIERS.get(amt, '?')}")

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"<span style='color:#6f7178'>{ts}</span>  {msg}")


def main():
    app = QApplication(sys.argv)
    ico = resource_path(ICON_FILE)
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    win = MainWindow(); win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
