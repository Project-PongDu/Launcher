#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gui.py  —  퐁듀 런처 : 치지직 → 좀보이드 후원연동 (단일 창, 치지직 공식 Open API)

설치:  pip install "chzzkpy>=2.2.0" PyQt5
실행:  python gui.py

구조 (양쪽 끝에 "돼지코(어댑터)"를 끼운 형태)
    DonationSource / ChzzkOfficialSource : 치지직 수신 추상화. chzzkpy(공식 API 모듈) 의존은
                                           ChzzkOfficialSource 안에만 존재.
    GameAdapter / ZomboidAdapter   : 게임별 출력(경로 탐지 + rewards.txt 기록). 게임 확장 포인트.
    DonationWorker                 : 코어. 스레드+asyncio로 Source 를 돌리고 Qt 시그널로 GUI에 전달.
    MainWindow                     : PyQt5 단일 창 UI.
    OptimizeDialog                 : 게임 최적화 설정 창. 자동 적용 없음 — PZ 설치 폴더를 고르고
                                     항목(힙 크기 / 좀비·청크 연산 패치 class)을 체크해 적용/해제.
                                     원본은 게임 폴더 puppet_opt_backup/ 에 최초 1회 자동 백업.

공식 Open API (v4.0.0 — 비공식 채팅 WS → 공식 세션 + 인증 중개 서버 전환):
    인증        : OAuth2. 게이트에서 브라우저 로그인 → localhost 콜백으로 code 수신 →
                  code 를 퐁듀 인증 서버(Cloudflare Workers, AUTH_SERVER)에 보내 토큰으로 교환.
                  Client Secret 은 인증 서버에만 존재 — 런처/exe 를 디컴파일해도 나오지 않는다.
                  Access Token(1일) + Refresh Token(30일·일회용). Refresh Token 은 config json
                  에 저장, 갱신될 때마다 즉시 재저장 → 재실행 시 무브라우저 자동 로그인.
    화이트리스트: 인증 서버가 토큰 발급/갱신과 한 몸으로 검사 (클라이언트에 검사 코드 없음).
                  목록에서 제거되면 다음 갱신(최대 1일) 시점에 403 → 연동 중단 + 게이트 복귀.
    수신 범위   : 공식 세션은 "로그인한 계정 본인 채널"의 후원 이벤트만 구독 가능.
                  → 임의 채널 입력 개념이 사라지고 게이트 1단계가 로그인으로 대체됨.
    19세 방송   : 공식 API 는 성인방송 여부와 무관하게 수신됨 — NID 쿠키/성인 게이트 전부 제거.
    방송 대기   : 세션은 방송 on/off 와 무관하게 유지됨 (방송 재시작 시 그대로 계속 수신).

연결 안정화 (장시간 방송 대응):
    [1] 프로토콜 heartbeat    : chzzkpy 공식 게이트웨이 자체 EIO3 ping 루프 + 수신 타임아웃
                                (ping_interval + ping_timeout) 사용.
    [2] Stale 워치독          : 서버발 모든 패킷 수신 시각을 스탬프. 90초간 아무것도 못 받으면
                                죽은 연결로 판정, 강제 재접속. (chzzkpy 게이트웨이는 소켓이
                                CLOSED 프레임만 뱉는 비정상 종료 시 스스로 못 빠져나오는 구멍이
                                있어 — 이 워치독이 막는다. 종료 시 ClientSession 강제 close 로
                                재접속 반복 시 세션 누수도 함께 막음)
    [3] 연결 타임아웃         : 세션 URL 발급→소켓→CONNECTED→후원 구독까지 20초 제한.
    [4] 지수 백오프           : 재접속 5→10→20→40→60초(최대). 연결 성공했던 시도 후엔 5초로 리셋.

라인 포맷(모드 DonationReceiver.lua 규약):  amount,featureId,sender,message
    (featureId/sender/message URL 인코딩·featureId는 reward_tiers 매핑에 없으면 빈 문자열로
    기록되며, 그 경우 모드 쪽에선 통계만 잡히고 게임 효과는 발동하지 않음)
"""

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
if os.name == "nt":
    import winsound   # 표준 라이브러리, Windows 전용 (배포 대상이 Windows 고정이라 조건부 임포트만 해둠)
else:
    winsound = None
from collections import namedtuple
from pathlib import Path

from PyQt5.QtCore import Qt, QObject, pyqtSignal, QTimer, QSharedMemory
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QComboBox,
    QTextEdit, QVBoxLayout, QHBoxLayout, QGridLayout, QFileDialog, QFrame,
    QCheckBox, QStackedWidget, QMessageBox, QDialog, QScrollArea, QProgressBar,
)


# ── 화이트리스트 ───────────────────────────────────────────────────────────────
# 시즌 참가 채널 검증은 퐁듀 인증 서버(pongdu-auth)가 토큰 발급/갱신과 한 몸으로 수행한다.
# 스트리머 추가/삭제는 지금처럼 Whitelist 레포의 JSON 만 커밋하면 됨 (반영 최대 60초).
# 런처(클라이언트)에는 화이트리스트 검사 코드가 존재하지 않는다 — 우회할 표면 자체가 없음.


VERSION = "v5.7.1"

# ── 치지직 공식 Open API 애플리케이션 정보 ─────────────────────────────────────
# 치지직 개발자센터(developers.naver.com/chzzk)에서 앱 등록 후 발급값을 채운다.
#   · 필요한 API Scope : "유저 정보 조회"(채널 확인용) + "후원 조회"(세션 이벤트 구독용)
#   · 로그인 리디렉션 URL 은 아래 OAUTH_REDIRECT 와 정확히 일치하게 등록해야 함
#   · Client Secret 은 이 파일/exe 에 존재하지 않는다 — 퐁듀 인증 서버(Cloudflare Workers)에만
#     있고, 토큰 발급/갱신은 전부 그 서버를 거친다. 서버가 화이트리스트를 함께 검사하므로
#     화이트리스트에서 제거된 채널은 다음 토큰 갱신(최대 1일) 시점부터 자동 차단된다.
CHZZK_CLIENT_ID     = "d10781fb-294a-4ed7-a7c2-c0d5d5445203"
AUTH_SERVER         = "https://pongdu-auth.t3qquq.workers.dev"   # 퐁듀 인증 중개 서버
AUTH_TIMEOUT        = 15.0    # 인증 서버 응답 대기 한도 (초)
OAUTH_PORT          = 51925
OAUTH_REDIRECT      = f"http://localhost:{OAUTH_PORT}/callback"
LOGIN_TIMEOUT       = 300.0   # 브라우저 로그인 대기 한도 (초)

# ── 자동 업데이트 ─────────────────────────────────────────────────────────────
# 배포처는 GitHub Releases. 릴리즈마다 asset 두 개를 올린다:
#     PongDu.exe      — 실행 파일 본체
#     version.json    — 아래 UPDATE_MANIFEST_URL 이 가리키는 매니페스트
# /releases/latest/download/<asset> 는 "최신 릴리즈의 그 asset" 으로 리다이렉트되는
# 고정 URL 이라, GitHub REST API 를 안 거친다 → 시간당 60회 rate limit 이 적용되지 않음.
#
# version.json 형식:
#     {
#       "version":       "v5.5.0",     필수. 최신 버전.
#       "url":           "https://github.com/.../download/v5.5.0/PongDu.exe",  필수.
#       "sha256":        "abc123…",    필수. url 이 가리키는 exe 의 해시(소문자 hex).
#       "min_supported": "v5.4.0",     선택. 이 버전 미만은 업데이트 강제(연기 불가).
#       "notes":         "변경점…"      선택. 다이얼로그에 그대로 표시.
#     }
# 나중에 인증 서버(pongdu-auth)로 옮기고 싶으면 UPDATE_MANIFEST_URL 만 바꾸면 된다.
UPDATE_MANIFEST_URL = "https://github.com/Project-PongDu/Launcher/releases/latest/download/version.json"
UPDATE_TIMEOUT      = 10.0    # 매니페스트 조회 한도 (초). 이 시간만큼은 조용히 실패해도 무방
UPDATE_DL_TIMEOUT   = 60.0    # exe 다운로드 소켓 타임아웃 (초)
# 다운로드 허용 호스트. 리다이렉트를 따라간 뒤 최종 URL 도 이 목록으로 재검증한다
# (매니페스트가 어떤 이유로든 오염돼도 임의 서버의 바이너리를 받아 실행하지 않도록).
UPDATE_ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com",
                        "release-assets.githubusercontent.com")
IS_FROZEN = bool(getattr(sys, "frozen", False))   # PyInstaller exe 로 실행 중인가







UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
# config.json 의 force_online=true 로 켜지는 관리자/테스트 모드.
# 게이트 체크리스트(방송 중 / 인게임 접속)를 전부 통과시킨다.
# 연동 중 감시(MainGuard)도 같은 헬퍼를 쓰므로 PZ 없이도 게이트로 안 튕긴다.
# 여기선 선언만 하고, 실제 값은 load_config() 정의 직후 아래에서 채운다.
FORCE_ONLINE = False

# ── 로컬 설정 (게임 유저 폴더 ~/Zomboid 안에 저장 -> rewards.txt 옆이라 찾기 쉬움) ──
def find_zomboid_dir() -> Path:
    """OS별 좀보이드 유저 데이터 폴더(.../Zomboid)를 찾는다.
       ZomboidAdapter.find_path()의 rewards.txt 탐지와 동일한 후보 순회 패턴."""
    home = Path.home()
    cands = [
        home / "Zomboid",
        Path(os.environ.get("USERPROFILE", home)) / "Zomboid",
    ]
    for env in ("OneDrive", "OneDriveConsumer"):
        od = os.environ.get(env)
        if od:
            cands.append(Path(od) / "Zomboid")
    for c in cands:
        if c.exists():
            return c
    for drive in ("C:", "D:", "E:", "F:"):
        base = Path(drive + "\\Users")
        if base.exists():
            try:
                for user in base.iterdir():
                    p = user / "Zomboid"
                    if p.exists():
                        return p
            except OSError:
                pass
    return cands[0]   # 못 찾으면 home/Zomboid (게임 실행 전이라 폴더가 아직 없을 수 있음)

CONFIG_NAME = "chzzk_donation_config.json"
CONFIG_DIR  = find_zomboid_dir()
CONFIG_PATH = CONFIG_DIR / CONFIG_NAME

def _read_json(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _alt_config_path(cfg: dict):
    """수동 지정된 Zomboid 폴더(zomboid_dir) 안의 설정 파일. 없으면 None.
       자동탐지 폴더와 실제 사용 폴더가 다를 때(예: ~/Zomboid 와 ~/'Zomboid (b41)'),
       사용자는 UI에 보이는 폴더의 설정 파일을 직접 편집하기 마련이라 그쪽도 봐야 한다."""
    alt = cfg.get("zomboid_dir", "")
    if not alt:
        return None
    p = Path(alt) / CONFIG_NAME
    if p == CONFIG_PATH or not p.exists():
        return None
    return p

# load_config()는 3초 폴링(pz_connected / 티어 감시 / force_online 재확인)에서 간접적으로
# 계속 불린다. 매번 JSON 을 읽고 파싱하면 PZ 가 청크를 스트리밍하는 같은 디스크에 초당
# 수 회의 read 를 얹는 꼴이라, 파일 내용이 안 바뀐 동안은 파싱 결과를 재사용한다.
# 무효화 기준은 (mtime_ns, size) — 외부 편집(직접 json 수정)도 그대로 감지된다.
_CFG_CACHE = None          # (sig, dict) | None
_CFG_SIG_MISS = object()   # 파일 없음을 표현하는 sentinel

def _file_sig(p: Path):
    try:
        st = p.stat()
    except OSError:
        return _CFG_SIG_MISS
    return (st.st_mtime_ns, st.st_size)

def invalidate_config_cache():
    global _CFG_CACHE
    _CFG_CACHE = None

def load_config() -> dict:
    """자동탐지 폴더의 설정을 기본값으로 읽고, 수동 지정 폴더에 설정 파일이 따로 있으면
       그 값으로 덮어쓴다 (수동 지정 폴더 우선).
       파일이 안 바뀌었으면 캐시된 파싱 결과의 사본을 돌려준다 (값은 전부 스칼라라 얕은
       복사로 충분하며, 호출부가 반환값을 수정해도 캐시가 오염되지 않는다)."""
    global _CFG_CACHE
    base_sig = _file_sig(CONFIG_PATH)

    if _CFG_CACHE is not None:
        sig, cached = _CFG_CACHE
        # alt 경로는 base 를 읽어야 알 수 있으므로, 캐시에 기록해 둔 경로로 검사한다.
        alt_path, alt_sig = sig[1], sig[2]
        if sig[0] == base_sig and (alt_path is None or _file_sig(alt_path) == alt_sig):
            return dict(cached)

    cfg = _read_json(CONFIG_PATH)
    alt = _alt_config_path(cfg)
    alt_sig = None
    if alt is not None:
        alt_sig = _file_sig(alt)
        cfg.update(_read_json(alt))
    _CFG_CACHE = ((base_sig, alt, alt_sig), dict(cfg))
    return cfg

def save_config(d: dict):
    """읽을 때 수동 지정 폴더 파일이 우선이므로, 저장은 존재하는 두 파일에 모두 한다.
       (한쪽만 갱신하면 오래된 값이 계속 새 값을 덮어써 버린다.)"""
    targets = [CONFIG_PATH]
    alt = _alt_config_path(d)
    if alt is not None:
        targets.append(alt)
    for p in targets:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    invalidate_config_cache()   # mtime 해상도에 기대지 않고 즉시 무효화

# 관리자/테스트 모드는 실행 시점에 1회 반영한다. (로그인 경로를 안 타고 게이트로
# 되돌아오는 흐름에서도 항상 적용되도록 _gate_pass 갱신에만 의존하지 않는다.)
FORCE_ONLINE = bool(load_config().get("force_online"))


# ── Zomboid 폴더 (자동탐지 or 수동지정) ─────────────────────────────────────
# pz_status.txt / pongdu_tiers.txt / rewards.txt 는 전부 같은 폴더
# (<Zomboid>/Lua/) 아래에 있다. 예전엔 이 셋을 각자 따로 후보 경로 순회로
# 찾았는데, 자동탐지가 틀리면(별도 드라이브 세이브, 특이한 OneDrive 리다이렉트
# 등) 셋 다 동시에 어긋난다. 이제 폴더 하나만 수동으로 바로잡으면 셋 다
# 한 번에 해결되도록, 이 폴더 하나에서 상대경로로 파생시킨다.
# CONFIG_DIR(설정 저장 위치) 자체는 자동탐지 실패해도 "쓸 수만 있으면" 되므로
# 그대로 두고, 파일 탐색용 폴더만 별도로 override 가능하게 분리한다.

def get_zomboid_dir() -> Path:
    """설정에 수동 지정된 폴더가 있으면 그걸, 없으면 자동탐지 결과(CONFIG_DIR)를 쓴다."""
    override = load_config().get("zomboid_dir", "")
    if override:
        p = Path(override)
        if p.exists():
            return p
    return CONFIG_DIR

def set_zomboid_dir_override(path: Path):
    cfg = load_config()
    cfg["zomboid_dir"] = str(path)
    save_config(cfg)

def zomboid_lua_path(filename: str) -> Path:
    """<Zomboid 폴더>/Lua/<filename> — pz_status.txt / pongdu_tiers.txt / rewards.txt 공용."""
    return get_zomboid_dir() / "Lua" / filename


# ── 리워드 프리셋 (레거시, 미사용) ──────────────────────────────────────────────
# 금액↔featureId 매핑 소유권이 모드 샌박(PongDu.Tier_<featureId>)으로 이전되면서
# 런처에서 이 로컬 파일을 읽거나/쓰는 코드 경로가 전부 사라졌다. 삭제하지 않고 남겨둔
# 이유는 나중에 "서버 설정이 없을 때 로컬로 폴백"이 필요해질 경우를 대비한 것뿐이다.
# 현재는 이 세 함수를 호출하는 곳이 없다.
PRESET_PATH = CONFIG_DIR / "reward_preset.json"

def load_reward_preset() -> dict:
    try:
        return json.loads(PRESET_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_reward_preset(tiers: dict):
    """{amount(int): featureId} -> reward_preset.json 기록. 저장 버튼 = 내보내기."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data = {str(amt): fid for amt, fid in sorted(tiers.items())}
        PRESET_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass

def reset_reward_preset():
    """프리셋 파일 삭제 -> 다음 로드부터 DEFAULT_REWARD_TIERS(코드 기본값) 사용."""
    try:
        PRESET_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# ── 채널 입력 정규화 ──────────────────────────────────────────────────────────
HEX32 = re.compile(r"[0-9a-fA-F]{32}")

def resource_path(rel):
    """exe(PyInstaller)로 묶였을 때든 그냥 실행이든 리소스 파일 경로를 찾는다."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)

ICON_FILE = "pongdu.ico"
SOUND_CONNECT_FILE = "connection.wav"   # 게이트 → 메인 전환 시 재생 (직접 준비해서 레포 루트에 넣을 것)

def play_connect_sound():
    """게이트에서 메인화면으로 넘어갈 때 알림음. 파일이 없거나 재생 실패해도
       연동 자체는 계속돼야 하므로 예외를 절대 밖으로 내보내지 않는다."""
    if winsound is None:
        return
    path = resource_path(SOUND_CONNECT_FILE)
    if not os.path.exists(path):
        return
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:
        pass

# ── 창 전환 ──────────────────────────────────────────────────────────────────
# 게이트 ↔ 메인은 서로를 속성으로 붙들고(close 만 호출) 있었기 때문에,
# gate1 → main1 → gate2 → main2 … 로 왕복할 때마다 이전 창의 위젯 트리가 하나도
# 해제되지 않고 체인으로 계속 살아 있었다 (Qt 의 close() 는 숨기기일 뿐이고,
# WA_DeleteOnClose 가 없으면 파괴되지 않는다). 백그라운드 스레드는 각 창의 closeEvent
# 에서 이미 정리되지만 위젯 자체가 남아, PZ 재접속을 반복하는 긴 방송에서 누적됐다.
# 현재 창은 이 전역이 하나만 붙들고, 이전 창은 닫은 뒤 삭제를 예약한다.
_ACTIVE_WINDOW = None

def swap_window(new_win, old_win=None):
    """new_win 을 화면 중앙에 띄우고, old_win 은 닫고 해제 예약한다.
       deleteLater 는 이벤트루프로 미루므로, 시그널 핸들러 안에서 호출해도 안전하다."""
    global _ACTIVE_WINDOW
    _ACTIVE_WINDOW = new_win
    center_on_screen(new_win)
    new_win.show()
    if old_win is not None:
        old_win.close()
        old_win.deleteLater()


def center_on_screen(win):
    """창을 현재 커서가 있는 모니터의 작업영역(작업표시줄 제외) 중앙에 배치.
       fixedSize 창은 OS 캐스케이딩이 안 먹어 show() 후 좌상단에 뜨는 경우가 있어 명시적으로 이동."""
    from PyQt5.QtWidgets import QApplication, QDesktopWidget
    from PyQt5.QtGui import QCursor
    screen = QApplication.desktop().screenNumber(QCursor.pos())
    if screen < 0:
        screen = QApplication.desktop().primaryScreen()
    geo = QDesktopWidget().availableGeometry(screen)
    fg = win.frameGeometry()
    fg.moveCenter(geo.center())
    win.move(fg.topLeft())

def extract_uuid(text: str):
    """입력 어디에 있든 32자리 hex(=채널 UUID)를 뽑는다. URL/라이브URL/생UUID 다 처리."""
    m = HEX32.search(text or "")
    return m.group(0).lower() if m else None


# ═══════════════════════════════════════════════════════════════════════════════
#  치지직 어댑터 (수신 방식 추상화 = "돼지코")
#  chzzkpy(공식 Open API 모듈) 의존은 ChzzkOfficialSource 안에만 존재.
# ═══════════════════════════════════════════════════════════════════════════════
Donation = namedtuple("Donation", "amount sender message")   # 플랫폼 중립 도네 1건


class SourceError(Exception):
    pass

class AuthRequired(SourceError):                # 로그인 없음 / refresh token 만료·무효 → 재로그인 필요
    pass

class NotWhitelisted(SourceError):              # 인증 서버가 화이트리스트 미등재로 거부 (403)
    def __init__(self, channel_name=""):
        super().__init__(channel_name)
        self.channel_name = channel_name

class StaleConnection(SourceError):             # 소켓은 열려있는 척하지만 수신이 끊긴 죽은 연결
    pass

class ConnectTimeout(SourceError):              # 제한 시간 안에 연결/구독 완료 못 함
    pass


class DonationSource:
    """치지직 수신 인터페이스. 이 4개만 구현하면 코어(DonationWorker)는 안 바뀐다."""

    async def resolve_channel(self, text):
        """수신 대상 채널 확정 -> (uuid, 표시이름). 못 하면 (None, 사유).
           공식 API 는 로그인 계정 본인 채널 고정이라 text 는 무시된다.
           저장된 로그인이 만료됐으면 AuthRequired 를 던진다."""
        raise NotImplementedError

    async def connect(self, uuid, emit, on_event=None):
        """연결 후 도네마다 emit(Donation) 호출. 정상 종료 시 리턴, 문제 시 SourceError.
           on_event(kind, detail): 연결 수명주기 통지 (같은 이벤트 루프에서 호출됨).
             kind ∈ connected / stale"""
        raise NotImplementedError

    def request_close(self):
        """스레드 세이프 종료 요청 플래그만 세움 (즉시 리턴). 실제 정리는 connect()가 수행."""
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError


# ── 연결 안정화 파라미터 ──────────────────────────────────────────────────────
STALE_SEC        = 90.0   # [2] 이 시간 동안 서버로부터 패킷을 하나도 못 받으면 죽은 연결로 판정
                          #     (정상 연결은 EIO3 ping/pong 으로 최소 ~25초마다 수신 발생)
STALE_CHECK_SEC  = 5.0    # [2] 워치독 검사 주기 (stop 요청 반응 속도도 이 주기)
CONNECT_TIMEOUT  = 20.0   # [3] 세션 URL 발급→소켓→CONNECTED→후원 구독까지 허용 시간
CLOSE_TIMEOUT    = 5.0    #     종료 정리 대기 한도

# 게이트웨이 received_message 래핑이 갱신하는 마지막 수신 시각.
# 이 앱은 동시에 1개 연결만 유지하므로 모듈 전역으로 충분 (다중 연결 확장 시 인스턴스화 필요).
_WS_ACTIVITY = {"t": 0.0}


class ChzzkOfficialSource(DonationSource):
    """치지직 공식 Open API(chzzkpy 2.2.0 공식 모듈) 기반 구현.
       ← 이 파일에서 chzzkpy 를 import/호출하는 유일한 곳.

    refresh_token : 저장돼 있던 리프레시 토큰 (없으면 브라우저 로그인 필요)
    on_token(tok) : 새 refresh token 발급 시 호출 — 일회용 토큰이라 매번 즉시 저장해야 함.
                    (워커 스레드에서 호출될 수 있으니 파일 저장 등 스레드 세이프한 작업만)
    """

    def __init__(self, refresh_token=None, on_token=None, grace_sec=3.0):
        self.grace = grace_sec
        self._refresh_token = refresh_token or None
        self._on_token = on_token
        self._client = None            # chzzkpy.Client (앱 단위)
        self._user = None              # chzzkpy.UserClient (로그인 유저 단위)
        self._closing = False          # request_close()가 세우는 스레드 세이프 플래그
        self.was_connected = False     # 직전 connect() 시도에서 구독 완료까지 갔는지 (백오프 리셋용)
        self._donation_sink = None     # 현재 connect() 의 도네 콜백 (연결 중에만 non-None)

    # ── 클라이언트/이벤트 준비 ──
    async def _ensure_client(self):
        if self._client is None:
            from chzzkpy import Client
            # Secret 은 인증 서버에만 있다. chzzkpy 의 토큰 발급 경로는 사용하지 않으므로
            # 빈 문자열을 넣는다 — 자동 갱신(refresh)은 _install_server_refresh 로 대체됨.
            self._client = Client(CHZZK_CLIENT_ID, "")
            self._bind_events(self._client)
        return self._client

    # ── 인증 서버 호출 (토큰 발급/갱신 + 화이트리스트 검사) ──
    async def _auth_server(self, path, payload):
        """퐁듀 인증 서버 호출. 성공 시 {accessToken, refreshToken, expiresIn, channelId, channelName}.
           401→AuthRequired / 403→NotWhitelisted / 그 외→SourceError."""
        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=AUTH_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(AUTH_SERVER + path, json=payload) as r:
                    try:
                        data = await r.json()
                    except Exception:
                        data = {}
                    if r.status == 200:
                        return data
                    if r.status == 401:
                        raise AuthRequired()
                    if r.status == 403:
                        raise NotWhitelisted(data.get("channelName") or "")
                    raise SourceError("인증 서버 오류 (%d): %s"
                                      % (r.status, data.get("detail") or data.get("error") or ""))
        except SourceError:
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
            raise SourceError("인증 서버 연결 실패: %s: %s" % (type(e).__name__, e)) from e

    async def _adopt_tokens(self, data):
        """인증 서버 응답의 토큰으로 UserClient 를 구성하고 채널 정보를 반영한다.
           채널은 인증 서버가 이미 확정했으므로(fetch_self 불필요) UserClient 를 직접 구성한다."""
        from chzzkpy.authorization import AccessToken
        from chzzkpy.client import UserClient
        client = await self._ensure_client()
        if client.http is None or not hasattr(client.loop, "create_task"):
            await client._async_setup_hook()               # loop/http 초기화 (initial_async_setup 과 동일 경로)
        at = AccessToken(
            access_token=data["accessToken"], refresh_token=data["refreshToken"],
            token_type="Bearer", expires_in=int(data.get("expiresIn") or 86400))
        self._user = UserClient(client, at)
        client.user_client.append(self._user)
        self._user.channel_id = data.get("channelId")
        self._user.channel_name = data.get("channelName") or ""
        self._install_server_refresh(self._user)
        self._store_token()
        if not self._user.channel_id:
            raise SourceError("채널 정보 확인 실패 — 인증 서버 응답에 channelId 없음")
        return self._user.channel_id, self._user.channel_name or ""

    def _install_server_refresh(self, user):
        """chzzkpy 의 자동 갱신(UserClient.refresh)이 Secret 으로 직접 갱신하려는 것을
           인증 서버 경유로 교체한다. 갱신마다 서버가 화이트리스트를 재검사하므로
           장시간 연동 중에도 회수가 관철된다."""
        import datetime
        source = self

        async def refresh():
            data = await source._auth_server(
                "/refresh", {"refreshToken": user.access_token.refresh_token})
            from chzzkpy.authorization import AccessToken
            at = AccessToken(
                access_token=data["accessToken"], refresh_token=data["refreshToken"],
                token_type="Bearer", expires_in=int(data.get("expiresIn") or 86400))
            user._connection.access_token = user.access_token = at
            user._token_generated_at = datetime.datetime.now()
            source._store_token()

        user.refresh = refresh

    def _bind_events(self, client):
        """chzzkpy 는 함수명으로 이벤트를 매칭한다 (dispatch("donation") → on_donation)."""
        source = self

        @client.event
        async def on_donation(message):
            sink = source._donation_sink
            if sink is not None:
                try:
                    sink(message)
                except Exception:
                    pass

    def _store_token(self):
        """현재 refresh token 을 밖으로 통지. 일회용이라 갱신 즉시 저장 안 하면 다음 실행 때 로그인 풀림."""
        u = self._user
        if u is None:
            return
        try:
            tok = u.access_token.refresh_token
        except Exception:
            return
        if tok and tok != self._refresh_token:
            self._refresh_token = tok
            if self._on_token:
                try:
                    self._on_token(tok)
                except Exception:
                    pass

    # ── 로그인 ──
    async def login_with_refresh(self):
        """저장된 refresh token 으로 무브라우저 로그인 (인증 서버 경유 — 화이트리스트 검사 포함).
           성공 시 (channel_id, 채널명)."""
        if not self._refresh_token:
            raise AuthRequired()
        data = await self._auth_server("/refresh", {"refreshToken": self._refresh_token})
        return await self._adopt_tokens(data)

    async def login_with_browser(self, cancel_event=None):
        """브라우저 OAuth2 로그인. localhost 콜백으로 code 를 받아 인증 서버에서 토큰으로 교환한다
           (화이트리스트 검사 포함). chzzkpy Client.login() 은 취소/타임아웃 시 서버 정리가 안 되고
           noconsole exe 에서 print 를 쓰는 문제가 있어 사용하지 않는다."""
        import secrets
        import webbrowser
        import aiohttp.web
        client = await self._ensure_client()

        state = secrets.token_urlsafe(16)
        result = {}
        got = asyncio.Event()

        async def _handle(request):
            result["code"] = request.query.get("code")
            result["state"] = request.query.get("state")
            got.set()
            return aiohttp.web.Response(
                text="로그인 완료! 이 창을 닫고 퐁듀 런처로 돌아가 주세요.",
                content_type="text/plain", charset="utf-8")

        web_app = aiohttp.web.Application()
        web_app.router.add_get("/callback", _handle)
        runner = aiohttp.web.AppRunner(web_app)
        await runner.setup()
        try:
            # localhost 가 ::1 로 먼저 해석되는 환경이 있어 IPv4/IPv6 양쪽에 바인드한다.
            # (IPv6 미지원 환경에서는 IPv4 단독으로 폴백)
            started = False
            for hosts in (["127.0.0.1", "::1"], "127.0.0.1"):
                try:
                    await aiohttp.web.TCPSite(runner, hosts, OAUTH_PORT).start()
                    started = True
                    break
                except OSError as e:
                    last_err = e
            if not started:
                raise SourceError("OAuth 콜백 포트(%d) 사용 불가 — 다른 프로그램이 점유 중: %s"
                                  % (OAUTH_PORT, last_err))

            url = client.generate_authorization_token_url(
                redirect_url=OAUTH_REDIRECT, state=state)
            webbrowser.open(url)

            waits = {asyncio.ensure_future(got.wait())}
            if cancel_event is not None:
                waits.add(asyncio.ensure_future(cancel_event.wait()))
            done, pending = await asyncio.wait(
                waits, timeout=LOGIN_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if cancel_event is not None and cancel_event.is_set():
                raise AuthRequired()                       # 사용자 취소 → 로그인 대기 상태로
            if not got.is_set():
                raise ConnectTimeout("%d초 안에 브라우저 로그인이 완료되지 않음"
                                     % int(LOGIN_TIMEOUT))
        finally:
            try:
                await runner.cleanup()                     # 콜백 서버는 어떤 경로든 반드시 내림
            except Exception:
                pass

        if result.get("state") != state or not result.get("code"):
            raise SourceError("OAuth 응답 비정상 (state 불일치 또는 code 없음)")
        data = await self._auth_server("/token", {"code": result["code"], "state": state})
        return await self._adopt_tokens(data)

    # ── DonationSource 구현 ──
    async def resolve_channel(self, text):
        # 공식 API 는 로그인 계정 본인 채널 고정 — text 는 사용하지 않는다.
        if self._user is not None and self._user.channel_id:
            return self._user.channel_id, self._user.channel_name or ""
        return await self.login_with_refresh()             # AuthRequired 는 그대로 전파

    async def connect(self, uuid, emit, on_event=None):
        from chzzkpy import UserPermission

        def ev(kind, detail=""):
            if on_event:
                try:
                    on_event(kind, detail)
                except Exception:
                    pass

        self._closing = False
        self.was_connected = False
        await self._ensure_client()
        if self._user is None:
            await self.login_with_refresh()                # AuthRequired 전파 → 워커가 게이트 복귀
        user = self._user

        started = {"t": time.monotonic()}                  # 도네 grace 기준점
        grace = self.grace

        def sink(message):                                 # chzzkpy Donation → 중립 Donation
            if time.monotonic() - started["t"] < grace:
                return                                     # 접속 직후 이벤트 방어 (리플레이 대비)
            try:
                amt = int(getattr(message, "pay_amount", 0) or 0)
            except (TypeError, ValueError):
                amt = 0
            if amt <= 0:
                return
            donator_id = getattr(message, "donator_id", "") or ""
            nick = (getattr(message, "donator_name", "") or "").strip()
            # 공식 API 규약: 익명 후원은 donatorChannelId == "anonymous", 닉네임 빈 문자열
            sender = "익명의 후원자" if (donator_id == "anonymous" or not nick) else nick
            body = (getattr(message, "donation_text", "") or "") \
                .replace("\r", " ").replace("\n", " ").strip()
            emit(Donation(amt, sender, body))

        self._donation_sink = sink
        try:
            # ── [3] 1단계: 세션 URL 발급 → 소켓 → CONNECTED → 후원 구독까지 타임아웃 ──
            try:
                await asyncio.wait_for(
                    user.connect(UserPermission(donation=True), addition_connect=True),
                    timeout=CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                raise ConnectTimeout("%d초 안에 연결되지 않음 (네트워크/치지직 서버 응답 없음)"
                                     % int(CONNECT_TIMEOUT))
            except SourceError:
                raise
            except Exception as e:
                self._raise_mapped(e)

            self.was_connected = True
            self._store_token()            # connect 경로의 자동 refresh 가 토큰을 갈았을 수 있음
            started["t"] = time.monotonic()
            _WS_ACTIVITY["t"] = time.monotonic()
            ev("connected")

            gw = getattr(user, "_gateway", None)
            read_task = getattr(gw, "_read_background_loop", None) if gw else None
            if gw is not None:             # [2] 수신 스탬프 — 이후 모든 패킷 수신 시각 기록
                _orig_recv = gw.received_message

                async def _recv_stamped(pkt):
                    _WS_ACTIVITY["t"] = time.monotonic()
                    return await _orig_recv(pkt)

                gw.received_message = _recv_stamped

            # ── [2] 2단계: 연결 유지 — stale 워치독 + 종료 요청 감시 ──
            while True:
                if read_task is not None:
                    done, _ = await asyncio.wait({read_task}, timeout=STALE_CHECK_SEC)
                else:
                    await asyncio.sleep(STALE_CHECK_SEC)
                    done = set()
                if self._closing:                          # 사용자 중지
                    return
                if read_task is not None and read_task in done:
                    exc = read_task.exception()
                    if exc is not None:
                        self._raise_mapped(exc)
                    return                                 # 서버측 정상 종료 → 워커가 재접속
                idle = time.monotonic() - _WS_ACTIVITY["t"]
                if idle > STALE_SEC:                       # 살아있는 척하는 죽은 연결
                    ev("stale", str(int(idle)))
                    raise StaleConnection("%d초간 서버 수신 없음" % int(idle))
        finally:
            self._donation_sink = None
            gw = getattr(user, "_gateway", None)
            try:                                           # 정상 종료 시도 (예외 무시)
                await asyncio.wait_for(user.disconnect(), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass
            # chzzkpy 게이트웨이 뒷정리 강제 수행:
            #  · disconnect 가 죽은 소켓에 send 하다 중간에 실패하면 태스크/소켓이 남는다
            #  · gateway 가 들고 있는 aiohttp.ClientSession 은 chzzkpy 가 절대 안 닫는다(누수)
            if gw is not None:
                try:
                    gw.is_connected = False
                    for tname in ("_ping_loop_task", "_read_background_loop"):
                        t = getattr(gw, tname, None)
                        if t is not None and not t.done():
                            t.cancel()
                    ws = getattr(gw, "websocket", None)
                    if ws is not None and not ws.closed:
                        await asyncio.wait_for(ws.close(), timeout=CLOSE_TIMEOUT)
                except Exception:
                    pass
                try:
                    sess = getattr(gw, "session", None)
                    if sess is not None and not sess.closed:
                        await sess.close()
                except Exception:
                    pass
            # UserClient 내부 상태 리셋 — disconnect 실패 시에도 다음 connect() 가 깨끗하게 시작
            try:
                user._gateway = None
                user._gateway_id = None
                user._session_id = None
                user._gateway_ready.clear()
            except Exception:
                pass
            self._store_token()

    # ── 예외 매핑 ──
    @staticmethod
    def _raise_mapped(e):
        """연결/수신 단계 예외 → 의미 있는 SourceError 로 분류 (로그 가독성 + 워커 분기)."""
        import aiohttp
        try:
            from chzzkpy.error import (
                UnauthorizedException, LoginRequired, ForbiddenException,
                TooManyRequestsException, ChatConnectFailed, ReceiveErrorPacket)
        except Exception:                                  # 라이브러리 구조 변경 대비
            UnauthorizedException = LoginRequired = ForbiddenException = ()
            TooManyRequestsException = ChatConnectFailed = ReceiveErrorPacket = ()
        if isinstance(e, (UnauthorizedException, LoginRequired)):
            raise AuthRequired() from e
        if isinstance(e, ForbiddenException):
            raise SourceError("호출 권한 없음(403) — 개발자센터 앱 API Scope('후원 조회') 확인 필요") from e
        if isinstance(e, TooManyRequestsException):
            raise SourceError("치지직 API 호출 제한(429) — 잠시 후 자동 재시도") from e
        if isinstance(e, ChatConnectFailed):
            raise SourceError("세션 접속 실패: %s" % e) from e
        if isinstance(e, ReceiveErrorPacket):
            raise SourceError("소켓 수신 오류 (서버측 종료 추정)") from e
        if isinstance(e, ConnectionError):
            raise SourceError("Heartbeat PONG 미수신 — 연결 유실") from e
        if isinstance(e, asyncio.TimeoutError):
            raise SourceError("소켓 타임아웃 (수신 두절)") from e
        if isinstance(e, asyncio.CancelledError):
            raise SourceError("수신 루프 취소됨") from e
        if isinstance(e, (aiohttp.ClientError, OSError)):
            raise SourceError("네트워크 오류: %s: %s" % (type(e).__name__, e)) from e
        raise SourceError("%s: %s" % (type(e).__name__, e)) from e

    def request_close(self):
        # 다른 스레드에서 호출됨 — 플래그만 세움. 워치독이 ≤STALE_CHECK_SEC 안에 감지해 정리.
        self._closing = True

    async def close(self):
        self._closing = True
        if self._user is not None:
            try:
                await asyncio.wait_for(self._user.disconnect(), timeout=CLOSE_TIMEOUT)
            except Exception:
                pass
        if self._client is not None and getattr(self._client, "http", None) is not None:
            try:
                await self._client.http.close()            # Open API HTTP 세션 정리
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
#  게임 어댑터 (출력 대상 추상화 = "돼지코")
# ═══════════════════════════════════════════════════════════════════════════════
class GameAdapter:
    name = "game"

    def __init__(self):
        self.path = None  # Path | None

    def find_path(self):
        raise NotImplementedError

    def write(self, amount, feature_id, sender, message):
        raise NotImplementedError


class ZomboidAdapter(GameAdapter):
    name = "좀보이드"

    # featureId -> 표시 라벨. rewardManager.lua의 rewardHandlers 키와 반드시 1:1로 일치해야 함
    # (mod_source.txt 기준 — 임의로 이름 바꾸지 말 것).
    FEATURES = {
        "buff_roulette":				"버프 룰렛",
        "debuff_roulette":				"디버프 룰렛",
        "random_weapon":				"랜덤 무기",
        "zombie_roulette":				"좀비 룰렛",
        "vaccine":						"백신",
        "vehicle_drop":					"차량 공중보급",
        "sprinter5":					"뛰좀 소환",
        "random_injury":				"랜덤 부상",
        "random_teleport":				"랜덤 텔레포트",
        "random_skill_potion":			"스킬 각성제",
        "mutant_spawn":					"특수좀비 소환",
        "inv_save_ticket":				"인벤토리 세이브 티켓",
        "food_supply":					"식량 보급",
        "instant_heal":					"즉시 치유",
        "fire_support":					"화력지원 룰렛",
        "missile":						"미사일 폭격",
        "zombie_rain":					"좀비 레인",
        "rise_up_dead_man":				"강령술",

        "medical_box":					"의약품 랜덤박스",
        "blood_moon":					"블러드문",
        "horde_night":					"호드나이트",

        # "revive_ticket":"즉시부활 티켓 (미구현)",
        # "secret_passage_kit":"비밀통로 키트 (미구현)",

        #미사용
        "bandit_melee":					"암살자 파견 (근접)",
        "bandit_ranged":				"암살자 파견 (원거리)",
        # "exile":"산타마을 유배 (삭제예정)",
        # "backroom":"백룸 (삭제예정)",
    }

    # 서버 전체에 영향을 주는 후원(서버후원). 여기 없는 featureId 는 전부 개인후원으로 취급한다.
    # 카테고리는 featureId 의 고정 속성이라 프리셋 파일 형식({amount: featureId})은 그대로다.
    SERVER_FEATURES = {"medical_box", "blood_moon", "horde_night"}

    @classmethod
    def feature_category(cls, fid):
        return "server" if fid in cls.SERVER_FEATURES else "personal"

    # 금액(원) -> featureId. 유저가 GUI에서 자유롭게 재배정 가능(reward_tiers).
    # 이 값은 config.json에 reward_tiers가 없을 때(첫 실행/구버전 마이그레이션)의 기본값.
    DEFAULT_REWARD_TIERS = {
        1000:   "buff_roulette",
        1100:   "debuff_roulette",
        2000:   "random_weapon",
        3000:   "zombie_roulette",
        5000:   "vaccine",
        7000:   "vehicle_drop",
        10000:  "sprinter5",
        15000:  "random_teleport",
        20000:  "random_skill_potion",
        30000:  "mutant_spawn",
        40000:  "inv_save_ticket",
        1500:   "food_supply",
        4000:   "instant_heal",
        4500:   "random_injury",
        50000:  "fire_support",
        60000:  "missile",
        70000:  "zombie_rain",
        80000:  "rise_up_dead_man",
        5500:   "medical_box",          # 서버후원
        66600:  "blood_moon",           # 서버후원
        100000: "horde_night",          # 서버후원
    }

    def __init__(self):
        super().__init__()
        # amount(int) -> featureId. MainWindow가 config.json 로드 후 덮어쓴다 (_load_reward_tiers).
        self.reward_tiers = dict(self.DEFAULT_REWARD_TIERS)

    def find_path(self):
        return zomboid_lua_path("rewards.txt")

    @staticmethod
    def _enc(s):
        # 콤마·줄바꿈·퍼센트만 인코딩, 한글 등 유니코드는 raw UTF-8로 통과 (PZ Lua urldecode 호환)
        return (s or "").replace("%", "%25").replace(",", "%2C").replace("\n", "%0A").replace("\r", "%0D")

    def write(self, amount, feature_id, sender, message):
        # featureId는 영문 소문자+언더스코어만 쓰므로 _enc 안 걸어도 됨 (콤마/개행 없음)
        line = "%d,%s,%s,%s" % (int(amount), feature_id or "", self._enc(sender), self._enc(message))
        if self.path is None:
            raise RuntimeError("rewards.txt 경로가 설정되지 않음")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        return line


# ═══════════════════════════════════════════════════════════════════════════════
#  런처 게이트용 헬퍼: 화이트리스트 / 방송 on-off / PZ 프로세스 감지
# ═══════════════════════════════════════════════════════════════════════════════
async def fetch_live(uuid: str) -> bool:
    """방송 on/off 판정 — 치지직 공개 폴링 엔드포인트 직접 호출 (로그인 불필요).
       게이트 UX 용도라 실패는 그냥 False (연동 자체는 방송 여부와 무관하게 유지됨)."""
    if FORCE_ONLINE:
        return True
    import aiohttp
    url = f"https://api.chzzk.naver.com/polling/v2/channels/{uuid}/live-status"
    try:
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(headers=UA, timeout=timeout) as s:
            async with s.get(url) as r:
                data = await r.json()
    except Exception:
        return False
    return (((data or {}).get("content") or {}).get("status")) == "OPEN"


def pz_running_real() -> bool:
    """FORCE_ONLINE 을 무시하고 실제 프로세스 목록만으로 확인.
    파일 교체 안전 체크(게임 최적화)처럼 '물리적으로 지금 떠 있는가'가 중요한 곳에서 쓴다."""
    KEY = "projectzomboid"
    try:                                         # psutil 있으면 우선 (의존성 아님, 있으면 사용)
        import psutil # pyright: ignore[reportMissingModuleSource]
        for p in psutil.process_iter(["name"]):
            if KEY in (p.info.get("name") or "").lower():
                return True
        return False
    except Exception:
        pass
    import subprocess
    if os.name == "nt":                          # 배포 대상: Windows
        try:
            CREATE_NO_WINDOW = 0x08000000        # noconsole exe 에서 콘솔창 안 뜨게
            out = subprocess.run(["tasklist"], capture_output=True, text=True,
                                 creationflags=CREATE_NO_WINDOW).stdout.lower()
            return KEY in out
        except Exception:
            return False
    try:                                         # 개발용 폴백 (mac/linux)
        out = subprocess.run(["pgrep", "-fil", KEY], capture_output=True, text=True).stdout.lower()
        return KEY in out
    except Exception:
        return False


# 참고: FORCE_ONLINE 을 존중하던 pz_running() 래퍼는 감시 폴링(MainGuard) 전용이었고,
# 그 폴링에서 프로세스 검사를 제거하면서 호출부가 전부 사라져 함께 삭제했다.
# 최적화 창의 파일 교체 안전 체크는 테스트 모드와 무관하게 실제 프로세스를 봐야 하므로
# 예전부터 pz_running_real() 을 직접 쓴다.


def pz_connected() -> bool:
    """pz_status.txt 읽어 인게임 접속 여부 확인.
    형식: CONNECTED|<unix timestamp>  — 10초 이상 갱신 없으면 False (Lua heartbeat 끊긴 것)."""
    if FORCE_ONLINE:
        return True
    TIMEOUT = 10
    c = zomboid_lua_path("pz_status.txt")
    if not c.exists():
        return False
    try:
        raw = c.read_text(encoding="utf-8").strip()
        if not raw.startswith("CONNECTED"):
            return False
        parts = raw.split("|")
        if len(parts) < 2:
            return False
        ts = float(parts[1])
        return (time.time() - ts) <= TIMEOUT
    except Exception:
        return False


# ── 서버 리워드 티어 (모드가 Zomboid/Lua/pongdu_tiers.txt 로 게시한 것을 그대로 읽음) ──
# 매핑을 "계산"하는 게 아니라 모드 샌박(Tier_<featureId>)이 이미 정해준 값을 읽어들이기만
# 한다. FEATURES 화이트리스트 검증만 방어적으로 거친다 — 판단 로직은 없음.

def load_server_tiers():
    """pongdu_tiers.txt -> (reward_tiers dict, server_name, ts) | (None, None, None).
       파일이 없거나 형식이 깨졌으면 None을 반환한다 (연동 차단 판단은 호출부 책임)."""
    p = zomboid_lua_path("pongdu_tiers.txt")
    if not p.exists():
        return None, None, None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None, None, None
    tiers = data.get("tiers")
    if not isinstance(tiers, dict) or not tiers:
        return None, None, None
    loaded = {}
    for k, v in tiers.items():
        try:
            amt = int(k)
        except (TypeError, ValueError):
            continue
        if amt > 0 and v in ZomboidAdapter.FEATURES:
            loaded[amt] = v
    if not loaded:
        return None, None, None
    return loaded, data.get("server", ""), data.get("ts")


# ═══════════════════════════════════════════════════════════════════════════════
#  PZ 최적화 (구 PZ_optimizer.zip 통합)
#  - 자동 적용 없음. '최적화 설정' 창에서 사용자가 항목을 골라 [적용]을 눌러야만 파일이 바뀐다.
#  - 항목 단위로 개별 적용/해제 가능 (체크 = 패치본, 해제 = 백업 원본 복원)
#  - JVM 힙을 전체 RAM의 절반으로: ProjectZomboid64.json(스팀 실행용) + .bat(직접 실행용) 패치
#  - 좀비 연산/청크 로딩 패치 class 9개 교체 (opt_conf/ 리소스, exe에 --add-data로 포함)
#  - 원본은 게임 폴더의 puppet_opt_backup/ 에 최초 1회 백업 → 체크 해제로 언제든 복원
#  - Program Files 등 쓰기 권한 없는 경로면 --pz-optimize 플래그로 자신을 관리자 재실행
#  주의: B41 전용 패치. 대상 원본이 없는 항목(B42 등)은 창에서 비활성으로 표시하고 건드리지 않음.
#
#  [항목을 "파일 1개 = 체크박스 1개"로 쪼개지 않은 이유]
#    IsoWorld$* 는 IsoWorld 의 내부 클래스라 서로 버전이 맞아야 한다. 하나만 패치본이고
#    나머지가 원본이면 런타임에 NoSuchMethodError/VerifyError 로 게임이 죽는다.
#    json/bat 도 마찬가지로 "힙 크기" 하나를 두 실행경로에 쓰는 것뿐이라, 한쪽만 바꾸면
#    bat 로 켠 유저는 여전히 3GB 로 돈다. 따라서 같이 움직여야만 하는 파일은 한 그룹으로
#    묶고, 그룹 안에 실제로 교체되는 파일 목록을 UI 에 그대로 노출한다.
# ═══════════════════════════════════════════════════════════════════════════════
OPT_DIRNAME = "opt_conf"
OPT_BACKUP_DIRNAME = "puppet_opt_backup"
PZ_APPID = "108600"

# key   : 설정 저장/CLI 인자용 식별자
# kind  : "class" = opt_conf 의 패치본으로 교체 / "heap" = 힙 인자 치환
# label : 체크박스 표시명
# files : 게임 폴더 기준 상대경로 (그룹 내 전부 함께 적용/해제)
OptGroup = namedtuple("OptGroup", "key kind label files")

OPT_GROUPS = [
    OptGroup("chunk", "class", "청크 로딩 최적화",
             ["zombie/iso/IsoChunkMap.class"]),
    OptGroup("world", "class", "월드 업데이트 / 좀비 정렬 연산 최적화",
             ["zombie/iso/IsoWorld.class",
              "zombie/iso/IsoWorld$CompDistToPlayer.class",
              "zombie/iso/IsoWorld$CompScoreToPlayer.class",
              "zombie/iso/IsoWorld$Frame.class",
              "zombie/iso/IsoWorld$MetaCell.class",
              "zombie/iso/IsoWorld$s_performance.class"]),
    OptGroup("netzombie", "class", "좀비 네트워크 패킷 최적화",
             ["zombie/popman/NetworkZombiePacker.class"]),
    OptGroup("zcount", "class", "좀비 개체수 연산 최적화",
             ["zombie/popman/ZombieCountOptimiser.class"]),
    OptGroup("heap", "heap", "JVM 힙 메모리 = 전체 RAM 의 절반",
             ["ProjectZomboid64.json", "ProjectZomboid64.bat"]),
]
OPT_GROUP_BY_KEY = {g.key: g for g in OPT_GROUPS}

_XMX_RE = re.compile(r"-Xmx\d+[mMgG]")
_XMS_RE = re.compile(r"-Xms\d+[mMgG]")


def opt_conf_dir():
    """패치 리소스 폴더(opt_conf). 빌드에 안 들어갔으면 None → 최적화 기능 전체 비활성."""
    p = Path(resource_path(OPT_DIRNAME))
    return p if p.is_dir() else None


def total_ram_mb() -> int:
    """전체 물리 RAM (MB). Windows는 GlobalMemoryStatusEx, 개발용(posix)은 sysconf."""
    if os.name == "nt":
        import ctypes
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys // (1024 * 1024))
        return 0
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except (ValueError, OSError, AttributeError):
        return 0


def half_ram_mb() -> int:
    """할당할 힙 크기 = 전체 RAM의 절반 (256MB 단위 절사, 최소 2048)."""
    total = total_ram_mb()
    if total <= 0:
        return 0
    return max(2048, (total // 2 // 256) * 256)


def _steam_roots():
    """Steam 루트 후보들: 레지스트리(HKCU 우선) → 드라이브 스캔. 중복 제거."""
    roots = []
    if os.name == "nt":
        try:
            import winreg
            for hive, key, val in (
                (winreg.HKEY_CURRENT_USER,  r"Software\Valve\Steam",              "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam",  "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam",              "InstallPath"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        v, _ = winreg.QueryValueEx(k, val)
                    p = Path(str(v))
                    if p.exists():
                        roots.append(p)
                except OSError:
                    pass
        except ImportError:
            pass
    for drive in ("C:", "D:", "E:", "F:", "G:", "H:"):
        for sub in ("Steam", "SteamLibrary",
                    "Program Files (x86)\\Steam", "Program Files\\Steam"):
            p = Path(drive + "\\") / sub
            if p.exists():
                roots.append(p)
    seen, out = set(), []
    for r in roots:
        key = str(r).lower()
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


_VDF_PATH_RE = re.compile(r'"path"\s+"((?:[^"\\]|\\.)*)"')

def _libraries_from(steam_root: Path):
    """steamapps/libraryfolders.vdf 파싱 → 이 Steam이 아는 모든 라이브러리 폴더."""
    libs = [steam_root]
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    try:
        raw = vdf.read_text(encoding="utf-8", errors="replace")
        for m in _VDF_PATH_RE.finditer(raw):
            libs.append(Path(m.group(1).replace("\\\\", "\\")))
    except OSError:
        pass
    return libs


def find_pz_dir():
    """PZ 설치 폴더 탐색. appmanifest_108600.acf가 있는 라이브러리 우선(별도 SteamLibrary 지원),
       없으면 폴더 존재만으로 폴백. 못 찾으면 None."""
    roots = _steam_roots()
    for root in roots:
        for lib in _libraries_from(root):
            steamapps = lib / "steamapps"
            if (steamapps / f"appmanifest_{PZ_APPID}.acf").exists():
                g = steamapps / "common" / "ProjectZomboid"
                if g.exists():
                    return g
    for root in roots:
        g = root / "steamapps" / "common" / "ProjectZomboid"
        if g.exists():
            return g
    return None


def pz_game_dir():
    """실제로 쓸 PZ 설치 폴더. 설정에 수동 지정(pz_dir)이 있으면 그걸 우선한다.
       (Zomboid 유저폴더와 달리 여기는 '게임이 설치된' 폴더 — ProjectZomboid64.json 이 있는 곳)"""
    override = load_config().get("pz_dir", "")
    if override:
        p = Path(override)
        if p.exists():
            return p
    return find_pz_dir()


def set_pz_dir_override(path):
    cfg = load_config()
    cfg["pz_dir"] = str(path) if path else ""
    save_config(cfg)


def is_pz_dir(p) -> bool:
    """최소 조건: ProjectZomboid64.json 이 있어야 우리가 아는 PZ 설치 폴더."""
    try:
        return p is not None and (Path(p) / "ProjectZomboid64.json").exists()
    except OSError:
        return False


def _backup_once(game_dir: Path, rel):
    """원본을 puppet_opt_backup/에 백업. 이미 백업본이 있으면 건드리지 않음(최초 원본 보존)."""
    src = game_dir / rel
    dst = game_dir / OPT_BACKUP_DIRNAME / rel
    if src.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _patched_vmargs(args, mb: int):
    """vmArgs에서 기존 -Xms/-Xmx 제거 후 새 값 삽입 (나머지 옵션·순서 보존)."""
    out = [a for a in args if not (str(a).startswith("-Xms") or str(a).startswith("-Xmx"))]
    return [f"-Xms{mb}m", f"-Xmx{mb}m"] + out


def _json_heap_mb(game_dir) -> int:
    """ProjectZomboid64.json 의 -Xmx 값(MB). 못 읽으면 0."""
    try:
        data = json.loads((game_dir / "ProjectZomboid64.json").read_text(encoding="utf-8"))
    except Exception:
        return 0
    for a in data.get("vmArgs", []):
        m = re.match(r"-Xmx(\d+)m$", str(a), re.I)
        if m:
            return int(m.group(1))
    return 0


# ── 항목(그룹) 단위 조회/적용/해제 ────────────────────────────────────────────
def opt_group_available(g, game_dir):
    """이 항목을 이 설치 폴더에 적용할 수 있는지. 반환: (가능여부, 불가사유)"""
    if game_dir is None:
        return False, "PZ 설치 폴더 없음"
    if g.kind == "class":
        conf = opt_conf_dir()
        if conf is None:
            return False, "패치 리소스(opt_conf) 없음"
        for rel in g.files:
            fname = rel.rsplit("/", 1)[-1]
            if not (conf / fname).exists():
                return False, f"패치 파일 누락: {fname}"
            # 패치본이 이미 덮여 있어도 파일 자체는 존재해야 한다. 없으면 구조가 다른 빌드(B42 등).
            if not (game_dir / rel).exists():
                return False, f"게임 파일 없음: {rel} (B41 아님?)"
        return True, ""
    # heap
    if not (game_dir / "ProjectZomboid64.json").exists():
        return False, "ProjectZomboid64.json 없음"
    if half_ram_mb() <= 0:
        return False, "RAM 크기를 알 수 없음"
    return True, ""


def opt_group_applied(g, game_dir) -> bool:
    """이 항목이 현재 '패치 상태'인지. 부분 적용은 False (적용 누르면 다시 맞춰진다)."""
    if game_dir is None:
        return False
    if g.kind == "class":
        conf = opt_conf_dir()
        if conf is None:
            return False
        for rel in g.files:
            fname = rel.rsplit("/", 1)[-1]
            src, dst = conf / fname, game_dir / rel
            try:
                if not dst.exists() or dst.read_bytes() != src.read_bytes():
                    return False
            except OSError:
                return False
        return True
    mb = half_ram_mb()
    if mb <= 0 or _json_heap_mb(game_dir) != mb:
        return False
    bpath = game_dir / "ProjectZomboid64.bat"
    if bpath.exists():
        try:
            raw = bpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        # bat 안의 -Xmx 는 여러 줄에 나온다. 하나라도 다른 값이면 미적용으로 본다
        # (json만 고치고 bat은 3GB 그대로 남는 케이스를 '적용됨'으로 오판하지 않기 위함)
        found = _XMX_RE.findall(raw)
        if not found or any(x.lower() != f"-xmx{mb}m" for x in found):
            return False
    return True


def apply_opt_group(g, game_dir):
    """항목 적용. 권한 문제는 PermissionError 그대로 던짐 → 호출자가 관리자 승격."""
    conf = opt_conf_dir()
    for rel in g.files:
        _backup_once(game_dir, Path(rel))
    if g.kind == "class":
        for rel in g.files:
            fname = rel.rsplit("/", 1)[-1]
            src, dst = conf / fname, game_dir / rel
            shutil.copy2(src, dst)
            if dst.read_bytes() != src.read_bytes():
                raise RuntimeError(f"복사 검증 실패: {fname}")
        return
    mb = half_ram_mb()
    # json (스팀 런처가 읽는 쪽): vmArgs의 Xms/Xmx만 교체, 나머지 보존
    jpath = game_dir / "ProjectZomboid64.json"
    data = json.loads(jpath.read_text(encoding="utf-8"))
    data["vmArgs"] = _patched_vmargs(data.get("vmArgs", []), mb)
    jpath.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # bat (직접 실행용): 모든 -Xms/-Xmx 치환 — 바닐라 bat는 1번째 실행 라인이 -Xmx3072m 고정이라
    # json만 고치면 bat 실행 유저는 3GB로 돌게 됨. 두 라인 다 치환해야 함.
    bpath = game_dir / "ProjectZomboid64.bat"
    if bpath.exists():
        raw = bpath.read_text(encoding="utf-8", errors="replace")
        raw = _XMX_RE.sub(f"-Xmx{mb}m", raw)
        raw = _XMS_RE.sub(f"-Xms{mb}m", raw)
        bpath.write_text(raw, encoding="utf-8")


def restore_opt_group(g, game_dir):
    """puppet_opt_backup/ 의 원본으로 되돌린다. 백업이 하나도 없으면 RuntimeError."""
    bdir = game_dir / OPT_BACKUP_DIRNAME
    n = 0
    for rel in g.files:
        src = bdir / rel
        if src.exists():
            dst = game_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            n += 1
    if n == 0:
        raise RuntimeError(f"'{g.label}' 백업이 없음 — Steam '게임 파일 무결성 검사'로 복원해 주세요")


def apply_opt_selection(game_dir, keys):
    """체크된 항목은 적용, 체크 해제된 항목은 원본 복원. 반환: (적용수, 복원수).
       적용 불가(available=False) 항목은 건드리지 않고 조용히 넘어간다."""
    keys = set(keys or [])
    applied = restored = 0
    for g in OPT_GROUPS:
        ok, _ = opt_group_available(g, game_dir)
        if not ok:
            continue
        now = opt_group_applied(g, game_dir)
        if g.key in keys:
            if not now:
                apply_opt_group(g, game_dir)
                applied += 1
        elif now:
            restore_opt_group(g, game_dir)
            restored += 1
    return applied, restored


def opt_applied_keys(game_dir):
    """현재 적용돼 있는 항목 key 집합 (체크박스 초기값)."""
    out = set()
    if game_dir is None:
        return out
    for g in OPT_GROUPS:
        ok, _ = opt_group_available(g, game_dir)
        if ok and opt_group_applied(g, game_dir):
            out.add(g.key)
    return out


def pz_optimize_state(game_dir):
    """('applied'|'partial'|'none', json의 Xmx MB). 적용 가능한 항목 기준으로 집계."""
    if game_dir is None:
        return ("none", 0)
    total = matched = 0
    for g in OPT_GROUPS:
        ok, _ = opt_group_available(g, game_dir)
        if not ok:
            continue
        total += 1
        if opt_group_applied(g, game_dir):
            matched += 1
    heap = _json_heap_mb(game_dir)
    if total and matched == total:
        return ("applied", heap)
    if matched > 0:
        return ("partial", heap)
    return ("none", heap)


def run_elevated_optimizer(game_dir, keys) -> bool:
    """관리자 권한으로 자기 자신을 --pz-optimize 로 재실행 (UAC 프롬프트).
       선택 상태를 그대로 넘겨야 하므로 폴더/키 목록을 인자로 전달한다.
       반환: 승격 프로세스 실행 성공 여부 (거부/실패 시 False)."""
    if os.name != "nt":
        return False
    import ctypes
    flag = f'--pz-optimize --pz-dir "{game_dir}" --pz-keys "{",".join(sorted(keys or []))}"'
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, flag
    else:
        exe = sys.executable
        params = f'"{os.path.abspath(__file__)}" {flag}'
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return int(r) > 32
    except Exception:
        return False


def _optimizer_cli(argv):
    """--pz-optimize 진입점 (승격 헬퍼). 단일 인스턴스 락을 안 잡으므로
       본체가 떠 있는 상태에서도 동작. 결과만 메시지박스로 알리고 종료."""
    _app = QApplication(sys.argv)

    def _arg(name):
        try:
            return argv[argv.index(name) + 1]
        except (ValueError, IndexError):
            return ""

    try:
        raw_dir = _arg("--pz-dir")
        game = Path(raw_dir) if raw_dir else pz_game_dir()
        if game is None or not is_pz_dir(game):
            raise RuntimeError("Project Zomboid 설치 폴더를 못 찾음")
        if pz_running_real():
            raise RuntimeError("Project Zomboid가 실행 중이라 파일을 교체할 수 없습니다.\n게임 종료 후 다시 시도해 주세요.")
        keys = [k for k in _arg("--pz-keys").split(",") if k]
        applied, restored = apply_opt_selection(game, keys)
        QMessageBox.information(None, "게임 최적화",
                                f"완료!\n\n적용 {applied}개 · 원본 복원 {restored}개\n경로: {game}")
        sys.exit(0)
    except Exception as e:
        QMessageBox.warning(None, "게임 최적화 실패", str(e))
        sys.exit(1)



# ═══════════════════════════════════════════════════════════════════════════════
#  코어: 수신 워커 (스레드 + asyncio -> Qt 시그널, 플랫폼/게임 중립)
# ═══════════════════════════════════════════════════════════════════════════════
class DonationWorker(QObject):
    donation = pyqtSignal(int, str, str)   # amount, sender, message
    status   = pyqtSignal(str, str)        # text, color(hex)
    resolved = pyqtSignal(str, str)        # uuid, display_name
    failed   = pyqtSignal(str)             # 멈춤
    note     = pyqtSignal(str)             # 로그용 (안 멈춤)
    auth_lost = pyqtSignal()               # 로그인 만료/무효 감지 (멈춤 + 게이트 복귀 → 재로그인)
    whitelist_lost = pyqtSignal()          # 연동 중 화이트리스트에서 제거됨 (멈춤 + 게이트 복귀)

    def __init__(self, source, channel_text="",
                 reconnect_sec=5.0, reconnect_max=60.0):
        super().__init__()
        self.source = source               # ← 어떤 수신 방식이든 DonationSource 만 받는다
        self.channel_text = channel_text   # 공식 API 소스는 사용 안 함 (로그인 계정 채널 고정)
        self.reconnect = reconnect_sec     # [4] 백오프 시작값 (연결 성공 후엔 여기로 리셋)
        self.reconnect_max = reconnect_max # [4] 백오프 상한
        self._backoff = reconnect_sec
        self._attempt = 0                  # 연속 실패 재접속 카운터 (성공 시 리셋)
        self._had_conn = False             # 이번 시도에서 CONNECTED 를 받았는지
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

    def _on_src_event(self, kind, detail=""):
        """소스 연결 수명주기 통지 → 상태/로그 릴레이. (소스 루프 스레드에서 호출되지만
           pyqtSignal.emit 은 스레드 세이프 — queued connection 으로 GUI 스레드에 전달됨)"""
        if kind == "connected":
            self._had_conn = True
            self._attempt = 0
            self.status.emit("연결됨", "#5dcaa5")
            self.note.emit("치지직 연결됨 ✓  (이 시점 이후의 후원부터 수신 — 방송 on/off 무관 유지)")
        elif kind == "stale":
            self.note.emit(f"⚠ Heartbeat/수신 두절 {detail}초 — 죽은 연결로 판정, 강제 재접속")

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        try:
            self.source.request_close()            # 스레드 세이프 플래그 — 워치독이 곧바로 감지
        except Exception:
            pass
        if self.loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.source.close(), self.loop)
                fut.result(timeout=3)
            except Exception:
                pass

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            uuid, name = self.loop.run_until_complete(
                self.source.resolve_channel(self.channel_text))
        except AuthRequired:
            self.auth_lost.emit()
            self.status.emit("대기 중", "#5f5e5a")
            return
        except NotWhitelisted:
            self.whitelist_lost.emit()
            self.status.emit("대기 중", "#5f5e5a")
            return
        except Exception as e:
            uuid, name = None, f"{type(e).__name__}: {e}"
        if not uuid:
            self.failed.emit(f"치지직 로그인/채널 확인 실패 ({name})")
            self.status.emit("대기 중", "#5f5e5a")
            return
        self.resolved.emit(uuid, name or "")

        while not self._stop:
            self._had_conn = False
            try:
                self.status.emit("연결 중…", "#ef9f27")
                if self._attempt > 0:
                    self.note.emit(f"재접속 시도 #{self._attempt}")
                self.loop.run_until_complete(
                    self.source.connect(uuid, self._emit, on_event=self._on_src_event))
                if self._stop:
                    break
                self._note_once("연결 끊김 — 재접속 대기 중")
                self.status.emit("재접속 대기…", "#ef9f27")
            except AuthRequired:
                # Access Token 만료(1일) 등 — 저장된 refresh 로 조용히 재로그인 1회 시도.
                # (갱신은 인증 서버가 화이트리스트를 재검사하므로 회수도 여기서 관철된다)
                try:
                    self.note.emit("로그인 갱신 중…")
                    self.loop.run_until_complete(self.source.login_with_refresh())
                    continue                       # 갱신 성공 → 백오프 없이 즉시 재접속
                except NotWhitelisted:
                    self.whitelist_lost.emit()
                    return
                except Exception:
                    self.auth_lost.emit()          # refresh 도 실패 → 게이트에서 재로그인
                    return
            except NotWhitelisted:
                self.whitelist_lost.emit()
                return
            except SourceError as e:
                if self._stop:
                    break
                self.note.emit(f"연결 끊김: {e}")
                self.status.emit("재접속 대기…", "#ef9f27")
            except Exception as e:
                if self._stop:
                    break
                self.note.emit(f"연결 오류: {type(e).__name__}: {e}")
                self.status.emit("재접속 대기…", "#ef9f27")

            # ── [4] 지수 백오프: 5→10→20→40→60(최대). 연결에 성공했던 시도 후엔 5초부터 다시 ──
            if self._had_conn:
                self._backoff = self.reconnect
                self._attempt = 0
            wait = self._backoff
            if not self._had_conn:
                self._backoff = min(self._backoff * 2, self.reconnect_max)
                self._attempt += 1
            self._note_once(f"{int(wait)}초 후 재접속…")
            self._sleep(wait)

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
QPushButton#start:disabled { background:#2b2e36; color:#5f5e5a; border:1px solid rgba(255,255,255,0.12); }
QPushButton#verify { background:#1d9e75; color:#04342c; border:none; font-weight:bold; padding:10px 24px; }
QPushButton#verify:hover { background:#22b384; }
QPushButton#verify:disabled { background:#2b2e36; color:#5f5e5a; border:1px solid rgba(255,255,255,0.12); }
QPushButton#stop  { background:#a32d2d; color:#ffe; border:none; font-weight:bold; padding:10px 20px; }
QPushButton#link  { background:transparent; border:none; color:#85b7eb; padding:2px; }
QPushButton#cat   { background:#2b2e36; border:1px solid rgba(255,255,255,0.12); color:#9a9ca3; padding:7px 20px; font-size:13px; }
QPushButton#cat:hover { background:#343843; color:#cfd0d4; }
QPushButton#cat:checked { background:#1d9e75; border:1px solid #1d9e75; color:#04342c; font-weight:bold; }
QCheckBox { color:#cfd0d4; font-size:12px; }
QLabel#muted { color:#9a9ca3; font-size:12px; }
QLabel#hint  { color:#6f7178; font-size:11px; }
QLabel#tier  { background:#2b2e36; border-radius:6px; padding:7px 10px; font-size:12px; color:#cfd0d4; }
QLabel#catsect { color:#5dcaa5; font-size:12px; font-weight:bold; padding:6px 2px 2px 2px; }
QLabel#brand { font-size:13px; font-weight:bold; color:#e8e8ea; }
QLabel#ver   { color:#6f7178; font-size:11px; }
QLabel#sect  { font-size:15px; font-weight:bold; color:#e8e8ea; }
QLabel#welcome { font-size:20px; color:#e8e8ea; }
QLabel#err   { font-size:18px; font-weight:bold; color:#e24b4a; }
QLabel#linkok { color:#5dcaa5; font-weight:bold; }
QFrame#sep { background:rgba(255,255,255,0.08); max-height:1px; }
"""


def make_header() -> QWidget:
    """모든 화면 상단 공용 바: 로고 + '치지직 API Launcher' + 버전."""
    bar = QWidget()
    h = QHBoxLayout(bar); h.setContentsMargins(0, 0, 0, 4); h.setSpacing(8)
    ico = resource_path(ICON_FILE)
    if os.path.exists(ico):
        pm = QPixmap(ico)
        if not pm.isNull():
            logo = QLabel()
            logo.setPixmap(pm.scaled(28, 28, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            h.addWidget(logo)
    brand = QLabel("치지직 API Launcher"); brand.setObjectName("brand")
    h.addWidget(brand); h.addStretch(1)
    ver = QLabel(VERSION); ver.setObjectName("ver")
    h.addWidget(ver)
    return bar



class RewardPresetDialog(QDialog):
    """리워드 프리셋 조회 창 — 더미(읽기 전용). 편집·불러오기·초기화·저장 기능은 삭제됨.

       금액 ↔ featureId 매핑의 소유권이 모드 샌박(Tier_<featureId>)으로 이전되면서,
       런처에서 이 값을 바꿀 방법 자체가 없어졌다. 여기서는 모드가 접속 시 게시한
       pongdu_tiers.txt 내용(=LauncherCore.server_tiers)을 그대로 나열만 한다.
       카테고리(개인/서버) 구분은 화면 표시용일 뿐 데이터에는 영향 없다."""

    CATEGORIES = (("personal", "개인후원"), ("server", "서버후원"))

    def __init__(self, parent=None, tiers=None, server_name=""):
        super().__init__(parent)
        self.setWindowTitle("리워드 프리셋 (서버 설정 — 읽기 전용)")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.setFixedSize(460, 620)
        self.tiers = dict(tiers or {})
        self.server_name = server_name or ""
        self._build()
        self.setStyleSheet(DARK_QSS)

    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18); root.setSpacing(10)
        hint = "이 서버의 금액↔기능 매핑입니다. 값은 서버장이 퐁듀 모드 샌드박스 설정에서" \
               " 지정하며, 런처에서는 편집할 수 없습니다."
        root.addWidget(self._muted(hint))
        if self.server_name:
            root.addWidget(self._muted(f"서버: {self.server_name}"))

        grid = QGridLayout()
        grid.setHorizontalSpacing(6); grid.setVerticalSpacing(4)
        host = QWidget(); host.setLayout(grid)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        for col, (cat, cat_label) in enumerate(self.CATEGORIES):
            head = QLabel(cat_label); head.setObjectName("catsect")
            grid.addWidget(head, 0, col)
            items = sorted((amt, fid) for amt, fid in self.tiers.items()
                           if ZomboidAdapter.feature_category(fid) == cat)
            for i, (amt, fid) in enumerate(items):
                label = ZomboidAdapter.FEATURES.get(fid, fid)
                l = QLabel(f"{amt:,} — {label}")
                l.setObjectName("tier")
                grid.addWidget(l, i + 1, col)
            grid.setColumnStretch(col, 1)
        if not self.tiers:
            grid.addWidget(self._muted("확인된 서버 설정이 없습니다."), 0, 0, 1, 2)
        grid.setRowStretch(grid.rowCount(), 1)

        close_btn = QPushButton("닫기"); close_btn.clicked.connect(self.accept)
        root.addWidget(close_btn)


class OptimizeDialog(QDialog):
    """게임 최적화 설정 창 — PZ 설치 폴더 지정 + 항목별 체크박스.

       자동 적용은 없다. 창을 열면 디스크의 현재 상태를 읽어 체크박스를 맞춰주고
       ([적용됨]=체크 / [미적용]=해제), [적용]을 눌렀을 때만 파일을 건드린다.
         · 체크했는데 미적용  → 패치본으로 교체 (원본은 puppet_opt_backup/ 에 최초 1회 백업)
         · 해제했는데 적용됨  → 백업 원본으로 복원
       쓰기 권한이 없으면(Program Files 등) 선택 상태를 그대로 인자로 넘겨 관리자 권한 재실행."""

    def __init__(self, parent=None, game_dir=None):
        super().__init__(parent)
        self.setWindowTitle("게임 최적화 설정")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.setFixedSize(670, 700)
        self.game_dir = game_dir
        self.boxes = {}          # key -> QCheckBox
        self._elevating = False  # 관리자 창에 넘긴 뒤 결과 재확인 대기중
        self._build()
        self.setStyleSheet(DARK_QSS)
        self._reload_state()

    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18); root.setSpacing(10)

        sect = QLabel("게임 최적화"); sect.setObjectName("sect")
        root.addWidget(sect)
        root.addWidget(self._muted(
            "체크한 항목만 게임 파일에 적용됩니다. 원본은 게임 폴더의\n"
            f"{OPT_BACKUP_DIRNAME}/ 에 백업되며, 체크를 해제하면 그 원본으로 되돌립니다."))

        drow = QHBoxLayout()
        drow.addWidget(self._muted("PZ 설치 폴더"))
        self.dir_input = QLineEdit(str(self.game_dir) if self.game_dir else "")
        self.dir_input.setReadOnly(True)
        drow.addWidget(self.dir_input, 1)
        pick = QPushButton("폴더 선택"); pick.setObjectName("link")
        pick.clicked.connect(self._choose_dir)
        drow.addWidget(pick)
        root.addLayout(drow)

        self.status = QLabel(""); self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        host = QWidget()
        self.list_v = QVBoxLayout(host)
        self.list_v.setContentsMargins(2, 2, 2, 2); self.list_v.setSpacing(2)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        for g in OPT_GROUPS:
            cb = QCheckBox(g.label)
            self.boxes[g.key] = cb
            self.list_v.addWidget(cb)
            files = QLabel("     " + "\n     ".join(g.files))
            files.setObjectName("hint")
            self.list_v.addWidget(files)
            self.list_v.addSpacing(6)
        self.list_v.addStretch(1)

        brow = QHBoxLayout()
        all_btn = QPushButton("전체 선택"); all_btn.setObjectName("link")
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("전체 해제"); none_btn.setObjectName("link")
        none_btn.clicked.connect(lambda: self._set_all(False))
        brow.addWidget(all_btn); brow.addWidget(none_btn); brow.addStretch(1)
        self.apply_btn = QPushButton("적용"); self.apply_btn.setObjectName("start")
        self.apply_btn.clicked.connect(self._apply)
        brow.addWidget(self.apply_btn)
        close_btn = QPushButton("닫기"); close_btn.clicked.connect(self.accept)
        brow.addWidget(close_btn)
        root.addLayout(brow)

    # --- 상태 ---
    def _set_all(self, on):
        for cb in self.boxes.values():
            if cb.isEnabled():
                cb.setChecked(on)

    def _reload_state(self):
        """디스크 현재 상태 → 체크박스/활성화/상태문구 반영."""
        self.dir_input.setText(str(self.game_dir) if self.game_dir else "")
        conf_missing = opt_conf_dir() is None
        for g in OPT_GROUPS:
            cb = self.boxes[g.key]
            ok, why = opt_group_available(g, self.game_dir)
            cb.setEnabled(ok)
            cb.setChecked(bool(ok and opt_group_applied(g, self.game_dir)))
            cb.setToolTip("" if ok else why)
            cb.setText(g.label if ok else f"{g.label}  —  {why}")

        msgs = []
        if conf_missing:
            msgs.append("패치 리소스(opt_conf)가 빌드에 포함되지 않아 class 교체 항목을 쓸 수 없습니다.")
        if self.game_dir is None:
            msgs.append("PZ 설치 폴더를 자동으로 찾지 못했습니다 — 폴더를 직접 지정해 주세요.")
        elif not is_pz_dir(self.game_dir):
            msgs.append("이 폴더에 ProjectZomboid64.json 이 없습니다 — PZ 설치 폴더가 맞는지 확인해 주세요.")
        mb = half_ram_mb()
        if mb > 0:
            msgs.append(f"설정될 힙 크기: {mb:,} MB (전체 RAM {total_ram_mb():,} MB 의 절반)")
        if pz_running_real():
            msgs.append("Project Zomboid 실행 중 — 게임을 종료해야 파일을 바꿀 수 있습니다.")
        self.status.setText("\n".join(msgs))
        self.apply_btn.setEnabled(self.game_dir is not None and is_pz_dir(self.game_dir))

    def _choose_dir(self):
        start = str(self.game_dir) if self.game_dir else str(Path.home())
        d = QFileDialog.getExistingDirectory(
            self, "Project Zomboid 설치 폴더 선택 (ProjectZomboid64.json 이 있는 폴더)", start)
        if not d:
            return
        p = Path(d)
        if not is_pz_dir(p):
            QMessageBox.warning(self, "게임 최적화",
                                "선택한 폴더에 ProjectZomboid64.json 이 없습니다.\n"
                                "…/steamapps/common/ProjectZomboid 폴더를 선택해 주세요.")
            return
        self.game_dir = p
        set_pz_dir_override(p)
        self._reload_state()

    # --- 적용 ---
    def _apply(self):
        if self.game_dir is None:
            return
        if pz_running_real():
            QMessageBox.information(self, "게임 최적화",
                                    "Project Zomboid가 실행 중이라 파일을 바꿀 수 없습니다.\n"
                                    "게임을 먼저 종료해 주세요.")
            return
        keys = [k for k, cb in self.boxes.items() if cb.isEnabled() and cb.isChecked()]
        try:
            applied, restored = apply_opt_selection(self.game_dir, keys)
        except PermissionError:
            # Program Files 등 쓰기 권한 없음 → 선택 상태 그대로 관리자 권한 프로세스에 위임
            if run_elevated_optimizer(self.game_dir, keys):
                self.status.setText("관리자 권한 창에서 처리 중…")
                self._elevating = True
                QTimer.singleShot(6000, self._reload_state)
            else:
                QMessageBox.warning(self, "게임 최적화", "실패 — 관리자 권한이 거부됐습니다.")
            return
        except Exception as e:
            QMessageBox.warning(self, "게임 최적화", f"실패: {e}")
            self._reload_state()
            return
        self._reload_state()
        if applied or restored:
            QMessageBox.information(self, "게임 최적화",
                                    f"완료!\n\n적용 {applied}개 · 원본 복원 {restored}개")
        else:
            QMessageBox.information(self, "게임 최적화", "변경할 항목이 없습니다.")


class MainWindow(QWidget):
    def __init__(self, preset=None):
        super().__init__()
        self.preset = preset or {}        # 런처에서 넘어온 {channel,uuid,name,autostart}
        self.setWindowTitle("PongDu Launcher  "+VERSION)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.resize(620, 1040)
        self.setFixedSize(620, 1040)
        self.adapter = ZomboidAdapter()
        self.worker = None
        self.cfg = load_config()
        self._returning = False                       # 게이트 복귀 중복 방지
        self.guard = None                             # PZ 종료 + 인게임 이탈 감시 (연동 중에만)
        self._tier_timer = None                       # pongdu_tiers.txt 변경 감시 (_build 이후 시작)
        self._tier_sig = None
        # 티어 편집 기능은 삭제됨 — 게이트가 이미 pongdu_tiers.txt에서 읽어 검증까지 끝낸
        # dict를 그대로 넘겨준다. 여기서는 계산도 재검증도 하지 않고 그대로 반영한다.
        self._load_reward_tiers()
        self.server_name = self.preset.get("server_name", "")
        self._build()
        self._restore()
        # pongdu_tiers.txt 변경 감시 (연동 중/대기 중 무관하게 항상 동작)
        self._tier_sig = self._tier_file_sig()
        self._tier_timer = QTimer(self)
        self._tier_timer.timeout.connect(self._watch_tiers)
        self._tier_timer.start(3000)
        if self.preset.get("autostart"):
            QTimer.singleShot(300, self._start)   # 창 뜨고 나서 워커 시작 (게이트에서 로그인 완료됨)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)


        title = QLabel("치지직 → 좀보이드 후원연동")
        title.setStyleSheet("font-size:15px; font-weight:bold;")
        root.addWidget(title)

        # 채널 — 공식 API 는 로그인 계정 본인 채널 고정 (게이트에서 로그인 완료 후 진입)
        name = self.preset.get("name", "")
        conn = QLabel(f"채널 연결됨: {name}" if name else "치지직 로그인 계정 채널로 연동됩니다")
        conn.setObjectName("linkok")
        root.addWidget(conn)

        # 경로 — Zomboid 폴더는 게이트에서 지정. 여기선 그 폴더 기준 고정 경로만 보여준다.
        root.addWidget(self._muted("rewards.txt 경로"))
        self.path_input = QLineEdit(); self.path_input.setReadOnly(True)
        root.addWidget(self.path_input)

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

        # 확정된 리워드 티어 (서버 설정 — 읽기 전용, 런처에서는 편집 불가)
        # 서버 티어가 중간에 바뀌면 라벨의 서버명도 따라가야 하므로 참조를 들고 있는다.
        self.tier_hint = self._muted("")
        root.addWidget(self.tier_hint)
        self._update_tier_hint()
        self.tiers_host = QWidget()
        self.tiers_host.setToolTip("서버장이 퐁듀 모드 샌드박스 설정에서 지정한 값입니다. 런처에서는 편집할 수 없습니다.")
        self.tiers_grid = QGridLayout(self.tiers_host)
        self.tiers_grid.setContentsMargins(0, 0, 6, 0)
        self.tiers_grid.setHorizontalSpacing(6); self.tiers_grid.setVerticalSpacing(4)
        tscroll = QScrollArea(); tscroll.setWidgetResizable(True)
        tscroll.setFrameShape(QFrame.NoFrame)
        tscroll.setWidget(self.tiers_host)
        tscroll.setToolTip("서버장이 퐁듀 모드 샌드박스 설정에서 지정한 값입니다. 런처에서는 편집할 수 없습니다.")
        tscroll.setFixedHeight(575)
        root.addWidget(tscroll)
        self._render_tier_display()

        root.addWidget(self._sep())

        # 테스트 후원
        trow = QHBoxLayout()
        trow.addWidget(self._muted("테스트 후원"))
        self.test_combo = QComboBox()
        self._build_test_combo()
        trow.addWidget(self.test_combo, 1)
        inject = QPushButton("확인"); inject.clicked.connect(self._inject_test)
        trow.addWidget(inject)
        root.addLayout(trow)

        # 로그
        root.addWidget(self._muted("실시간 도네 로그"))
        self.log = QTextEdit(); self.log.setReadOnly(True); self.log.setMinimumHeight(140)
        # 상한이 없으면 장시간 방송에서 후원 로그가 무한히 쌓여 문서가 커지고, 새 줄이 붙을
        # 때마다 리레이아웃 비용이 함께 커진다. 오래된 줄은 자동으로 버린다.
        self.log.document().setMaximumBlockCount(400)
        root.addWidget(self.log, 1)

        self.setStyleSheet(DARK_QSS)

    # --- 헬퍼 ---
    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _sep(self):
        f = QFrame(); f.setObjectName("sep"); f.setFixedHeight(1); return f

    def _update_tier_hint(self):
        hint = "리워드 티어  —  서버 설정 (편집 불가)"
        if self.server_name:
            hint += f"  ·  {self.server_name}"
        self.tier_hint.setText(hint)

    # --- 서버 티어 실시간 반영 ---
    def _tier_file_sig(self):
        """pongdu_tiers.txt 의 (mtime_ns, size). 없거나 못 읽으면 None."""
        try:
            st = zomboid_lua_path("pongdu_tiers.txt").stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _watch_tiers(self):
        """서버장이 게임 중 샌드박스 Apply 를 누르면 모드가 pongdu_tiers.txt 를 다시 쓴다.
           게이트에서 뜬 스냅샷이 그대로 굳지 않도록 메인화면에서도 따라간다.
           매 주기 전체 파싱하지 않고 mtime/size 가 바뀐 경우에만 읽는다."""
        sig = self._tier_file_sig()
        if sig is None or sig == self._tier_sig:
            return
        self._tier_sig = sig
        tiers, server, _ts = load_server_tiers()
        if not tiers:
            # 모드가 쓰는 도중이거나 형식이 깨진 상태. 표시를 비우면 후원 처리 기준까지
            # 흔들리므로 기존 값을 그대로 유지한다 (다음 주기에 다시 시도됨).
            return
        server = server or ""
        if tiers == self.adapter.reward_tiers and server == self.server_name:
            return
        self.adapter.reward_tiers = tiers
        self.server_name = server
        # 게이트 복귀 후 재진입 시에도 최신값이 쓰이도록 preset 스냅샷도 같이 갱신
        self.preset["reward_tiers"] = dict(tiers)
        self.preset["server_name"] = server
        self._update_tier_hint()
        self._render_tier_display()
        self._refresh_test_combo()
        self._log(f"서버 리워드 티어 갱신됨 ({len(tiers)}개).")

    def _refresh_test_combo(self):
        """티어 변경 후 테스트 콤보 재구성. 선택 중이던 금액이 남아 있으면 유지한다."""
        keep = self.test_combo.currentData()
        self._build_test_combo()
        if keep is not None:
            i = self.test_combo.findData(keep)
            if i >= 0:
                self.test_combo.setCurrentIndex(i)

    def _render_tier_display(self):
        """adapter.reward_tiers -> 읽기 전용 2열 라벨 그리드 (금액 오름차순).
           왼쪽 칼럼 = 개인후원, 오른쪽 칼럼 = 서버후원. 칼럼 순서는 CATEGORIES 정의 순."""
        while self.tiers_grid.count():
            item = self.tiers_grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for col, (cat, cat_label) in enumerate(RewardPresetDialog.CATEGORIES):
            head = QLabel(cat_label); head.setObjectName("catsect")
            self.tiers_grid.addWidget(head, 0, col)
            items = sorted((amt, fid) for amt, fid in self.adapter.reward_tiers.items()
                           if self.adapter.feature_category(fid) == cat)
            for i, (amt, fid) in enumerate(items):
                label = self.adapter.FEATURES.get(fid, fid)
                l = QLabel(f"{amt:,} — {label}")
                l.setObjectName("tier")
                self.tiers_grid.addWidget(l, i + 1, col)
            # 항목 수가 다른 쪽 칼럼이 세로로 늘어나지 않도록 남는 공간을 아래로 밀어둔다
            self.tiers_grid.setColumnStretch(col, 1)
        self.tiers_grid.setRowStretch(self.tiers_grid.rowCount(), 1)

    def _build_test_combo(self):
        """테스트 후원 목록. 카테고리 구분은 라벨 접미사로만 (콤보에 그룹 헤더를 넣으면
           선택 불가 더미 항목이 생겨 currentData() 로직에 방어 코드가 붙는다)."""
        self.test_combo.clear()
        # 개인후원 전체(금액 오름차순) → 서버후원 전체(금액 오름차순) 순서.
        # 금액과 무관하게 카테고리가 1차 정렬키다 (서버후원은 금액이 낮아도 항상 아래).
        def _order(item):
            amt, fid = item
            return (1 if self.adapter.feature_category(fid) == "server" else 0, amt)
        for amt, fid in sorted(self.adapter.reward_tiers.items(), key=_order):
            label = self.adapter.FEATURES.get(fid, fid)
            if self.adapter.feature_category(fid) == "server":
                label += "  (서버)"
            self.test_combo.addItem(f"{amt:,} — {label}", amt)

    # --- 설정 복원/저장 ---
    def _load_reward_tiers(self):
        """게이트(LauncherCore)가 pongdu_tiers.txt에서 이미 읽고 검증한 dict를 그대로
           반영한다. 여기서 다시 파싱하거나 판단하지 않는다 — 게이트가 연동 시작을 막지
           않았다면 이 값은 항상 존재한다."""
        loaded = self.preset.get("reward_tiers") or {}
        if loaded:
            self.adapter.reward_tiers = loaded

    def _restore(self):
        """rewards.txt 경로는 이제 파일 단위로 수동 지정하지 않는다 — 게이트에서
           지정한 Zomboid 폴더 하나로부터 항상 같은 상대경로로 고정된다."""
        p = self.adapter.find_path()
        self.adapter.path = p
        self.path_input.setText(str(p))

    def closeEvent(self, e):
        if self._tier_timer is not None:
            self._tier_timer.stop()
        self._kill_guard()          # 창을 X로 닫는 경로에선 _back_to_gate 를 안 타므로 여기서도 정리
        if self.worker:
            self.worker.stop()
        super().closeEvent(e)

    # --- 시작/중지 ---
    def _toggle(self):
        if self.worker is None:
            self._start()
        else:
            self._back_to_gate(manual=True)   # 중지 누르면 완전 초기 게이트로 복귀 (자동 재진입 없음)

    def _save_token(self, tok):
        """워커 스레드에서 호출됨 — refresh token 갱신 즉시 저장 (Qt 객체 접근 금지)."""
        _persist_refresh_token(tok)

    def _start(self):
        if self.adapter.path is None:
            self._log("rewards.txt 경로가 없음. 게이트의 ‘Zomboid 폴더’에서 지정해 주세요."); return
        source = ChzzkOfficialSource(                  # ← 수신 어댑터 (치지직 공식 Open API)
            refresh_token=load_config().get("chzzk_refresh_token", ""),
            on_token=self._save_token)
        self.worker = DonationWorker(source)
        self.worker.donation.connect(self._on_donation)
        self.worker.status.connect(self._on_status)
        self.worker.resolved.connect(self._on_resolved)
        self.worker.failed.connect(self._on_failed)
        self.worker.note.connect(self._log)
        self.worker.auth_lost.connect(self._auth_to_gate)   # 로그인 만료 → 게이트에서 재로그인
        self.worker.whitelist_lost.connect(self._wl_to_gate) # 시즌 목록 제거 감지 → 게이트 복귀
        self.worker.start()
        self.start_btn.setText("중지"); self.start_btn.setObjectName("stop"); self.setStyleSheet(DARK_QSS)
        uuid = self.preset.get("uuid")
        if self.preset.get("autostart") and uuid:      # 게이트를 거쳐 들어온 경우만 감시
            self.guard = MainGuard(uuid)               # PZ 종료/인게임 이탈을 짧은 주기로 직접 폴링
            self.guard.pz_lost.connect(self._pz_to_gate)
            self.guard.start()
        self._log("연동 시작…")

    def _kill_guard(self):
        if self.guard is not None:
            self.guard.shutdown(); self.guard = None

    def _stop(self):
        self._kill_guard()
        if self.worker:
            self.worker.stop(); self.worker = None
        self.start_btn.setText("연동 시작"); self.start_btn.setObjectName("start"); self.setStyleSheet(DARK_QSS)
        self._on_status("대기 중", "#5f5e5a")
        self._log("중지됨.")

    def _back_to_gate(self, warn_auth=False, warn_wl=False, manual=False):
        """워커 정리하고 게이트 창으로 돌아간다 (중지 / PZ 종료 / 로그인 만료 / 미등재 공통).
           warn_auth=True 면 저장 토큰을 지우고 경고창 후 로그인 화면부터 다시 시작.
           warn_wl=True 면 미등재 경고 후 게이트로 (게이트 자동 로그인이 미등재 화면을 띄운다).
           manual=True (스트리머가 직접 '중지'를 누른 경우) 면 새 게이트에서 자동 연동시작을
           끈다. 그 외 경로(PZ 종료 / 로그인 만료 / 미등재)는 사용자가 의도한 중단이 아니므로
           체크리스트가 다시 전부 초록이 되면 최초 실행 때처럼 자동으로 연동을 시작한다.
           _returning 을 먼저 세워 경고창 중 중복 트리거를 막는다."""
        if self._returning:
            return
        self._returning = True
        self._kill_guard()
        if self.worker:
            self.worker.stop(); self.worker = None
        if warn_auth:
            _clear_refresh_token()
            QMessageBox.warning(self, "로그인 만료",
                                "치지직 로그인이 만료됐습니다.\n연동을 중단하고 로그인 화면으로 돌아갑니다.")
        elif warn_wl:
            QMessageBox.warning(self, "시즌 참가 목록 변경",
                                "채널이 시즌 참가 목록에서 제외되어 연동이 중단됩니다.")
        preset = None
        if not (warn_auth or warn_wl) and self.preset.get("uuid"):
            preset = {"uuid": self.preset["uuid"], "name": self.preset.get("name", "")}
        swap_window(LauncherWindow(preset=preset, auto_advance=not manual), self)

    def _pz_to_gate(self):
        if self._returning:
            return
        self._log("Project Zomboid 종료 감지 — 게이트로 돌아갑니다.")
        self._back_to_gate()

    def _auth_to_gate(self):
        self._back_to_gate(warn_auth=True)

    def _wl_to_gate(self):
        self._back_to_gate(warn_wl=True)

    # --- 시그널 핸들러 ---
    def _on_donation(self, amount, sender, message):
        feature_id = self.adapter.reward_tiers.get(amount, "")
        self.adapter.write(amount, feature_id, sender, message)
        if feature_id:
            label = self.adapter.FEATURES.get(feature_id, feature_id)
            self._log(f"{sender}  {amount:,}원  →  {label}")
        else:
            self._log(f"{sender}  {amount:,}원  (통계만)")

    def _on_status(self, text, color):
        self.status_text.setText(text)
        self.status_dot.setStyleSheet(f"color:{color}; font-size:14px;")

    def _on_resolved(self, uuid, name):
        short = f"{uuid[:8]}…{uuid[-3:]}"
        label = name if name else short
        self._log(f"채널 인식됨 · {label}")

    def _on_failed(self, msg):
        self._log("⚠ " + msg)
        self._stop()

    def _inject_test(self):
        amt = self.test_combo.currentData()
        if amt is None:
            self._log("테스트할 티어가 없음. 리워드 티어를 먼저 저장해 주세요."); return
        if self.adapter.path is None:
            self._log("경로가 없어 테스트 불가. 경로를 먼저 지정해 주세요."); return
        feature_id = self.adapter.reward_tiers.get(amt, "")
        self.adapter.write(amt, feature_id, "테스트후원자", "테스트")
        label = self.adapter.FEATURES.get(feature_id, feature_id or "?")
        self._log(f"[테스트] {amt:,}원 적용  →  {label}")

    def _log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"<span style='color:#6f7178'>{ts}</span>  {msg}")


# ═══════════════════════════════════════════════════════════════════════════════
#  런처 게이트: 채널확인 → 화이트리스트 → 방송 → PZ → 연동시작 → 메인창
# ═══════════════════════════════════════════════════════════════════════════════
def _persist_refresh_token(tok: str):
    """새 refresh token 즉시 저장 (일회용이라 유실 시 다음 실행 때 재로그인 필요).
       워커/코어 스레드에서 호출되므로 Qt 객체는 건드리지 않는다."""
    cfg = load_config()
    cfg["chzzk_refresh_token"] = tok
    save_config(cfg)


def _clear_refresh_token():
    cfg = load_config()
    if cfg.pop("chzzk_refresh_token", None) is not None:
        save_config(cfg)


class LauncherCore(QObject):
    """게이트용 비동기 워커. 치지직 로그인/화이트리스트 검증 + 방송·PZ 폴링을 한 루프에서 돌린다."""
    resolved = pyqtSignal(str, str)   # uuid, name (로그인 + 화이트리스트 통과)
    invalid  = pyqtSignal()           # 화이트리스트 미등재
    login_needed = pyqtSignal(str)    # 자동 로그인 실패/취소 → 로그인 버튼 표시 (detail=사유, 빈 문자열 가능)
    live     = pyqtSignal(bool)       # 방송 on/off
    connected = pyqtSignal(bool)      # PZ 연결 상태
    tiers    = pyqtSignal(bool)       # 서버 리워드 티어(pongdu_tiers.txt) 확인 여부

    def __init__(self):
        super().__init__()
        self.loop = None
        self._uuid = None
        self._polling = False
        # 모드가 접속 시 게시한 서버 티어 — read_server_tiers()가 채우고, 인게임 접속이
        # 끊기면 비운다. reward_tiers는 여기서 "계산"하는 게 아니라 이 값을 그대로 읽어들인 것.
        self.server_tiers = None
        self.server_name = ""
        self.server_ts = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    # --- 치지직 로그인 ---
    def auto_login(self):
        """실행 직후: 저장된 refresh token 으로 무브라우저 로그인 시도."""
        self._submit(self._auto_login())

    def browser_login(self):
        """로그인 버튼: 브라우저 OAuth 로그인."""
        self._login_cancel = asyncio.Event()
        self._submit(self._browser_login())

    def cancel_login(self):
        ev = getattr(self, "_login_cancel", None)
        if ev is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(ev.set)

    async def _auto_login(self):
        tok = load_config().get("chzzk_refresh_token", "")
        if not tok:
            self.login_needed.emit("")
            return
        source = ChzzkOfficialSource(refresh_token=tok, on_token=_persist_refresh_token)
        try:
            uuid, name = await source.login_with_refresh()
        except AuthRequired:
            _clear_refresh_token()                 # 만료/무효 토큰은 지워서 다음부터 바로 버튼 표시
            self.login_needed.emit("저장된 로그인이 만료됐습니다. 다시 로그인해 주세요.")
            return
        except NotWhitelisted:
            self.invalid.emit()                    # 로그인 자체는 유효 — 토큰은 유지
            return
        except Exception as e:
            self.login_needed.emit(f"자동 로그인 실패: {e}")
            return
        finally:
            await source.close()
        self._gate_pass(uuid, name)

    async def _browser_login(self):
        source = ChzzkOfficialSource(on_token=_persist_refresh_token)
        try:
            uuid, name = await source.login_with_browser(cancel_event=self._login_cancel)
        except AuthRequired:                       # 사용자 취소
            self.login_needed.emit("")
            return
        except NotWhitelisted:
            self.invalid.emit()
            return
        except Exception as e:
            self.login_needed.emit(f"로그인 실패: {e}")
            return
        finally:
            await source.close()
        self._gate_pass(uuid, name)

    def _gate_pass(self, uuid, name):
        """로그인 + 화이트리스트 통과 (검사는 인증 서버가 토큰 발급과 한 몸으로 수행)."""
        global FORCE_ONLINE
        FORCE_ONLINE = bool(load_config().get("force_online"))   # 관리자/테스트 모드 (config 플래그)
        self.resolved.emit(uuid, name or "")

    # --- 방송 / PZ 폴링 (체크리스트) ---
    def start_poll(self, uuid):
        self._uuid = uuid
        if not self._polling:
            self._submit(self._poll())

    async def _poll(self):
        global FORCE_ONLINE
        self._polling = True
        while self._polling:
            # 테스트 편의: 게이트 체크리스트가 떠 있는 동안에도 config 의 force_online
            # 변경이 즉시 반영되도록 매 주기 다시 읽는다 (작은 JSON 이라 비용 무시 가능).
            FORCE_ONLINE = bool(load_config().get("force_online"))
            live = await fetch_live(self._uuid)
            self.live.emit(live)
            # PZ 실행 여부는 따로 확인하지 않는다 — 인게임 접속(pz_status.txt heartbeat)이
            # 확인되면 프로세스는 당연히 떠 있으므로, 3초마다 도는 tasklist 조회만 낭비였다.
            try:
                connected = await self.loop.run_in_executor(None, pz_connected)
            except Exception:
                connected = False
            self.connected.emit(connected)

            # 인게임 접속이 확인된 동안에만 서버 티어를 갱신한다. 접속이 끊기면 캐시를
            # 비워서, 재접속 전까지는 "확인됨" 상태로 남지 않게 한다 (연동 차단 정책).
            if connected:
                try:
                    tiers, server, ts = await self.loop.run_in_executor(None, load_server_tiers)
                except Exception:
                    tiers, server, ts = None, None, None
                self.server_tiers = tiers
                self.server_name = server or ""
                self.server_ts = ts
                self.tiers.emit(tiers is not None)
            else:
                self.server_tiers = None
                self.server_name = ""
                self.server_ts = None
                self.tiers.emit(False)

            await asyncio.sleep(3)

    def stop_poll(self):
        self._polling = False

    def shutdown(self):
        self._polling = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)


class MainGuard(QObject):
    """메인 연동 중 감시 워커: PZ 종료 + 인게임 이탈을 폴링해서 시그널만 쏜다."""
    pz_lost  = pyqtSignal()

    def __init__(self, uuid):
        super().__init__()
        self.uuid = uuid
        self.loop = None
        self._polling = False
        self._conn_misses = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    def start(self):
        self._polling = True
        asyncio.run_coroutine_threadsafe(self._poll(), self.loop)

    async def _poll(self):
        while self._polling:
            # PZ 프로세스 존재 여부는 따로 확인하지 않는다. 게이트 폴링(LauncherCore._poll)에서
            # 같은 이유로 이미 제거한 검사이며, pz_connected() 의 heartbeat 가 10초 타임아웃으로
            # 끊기면 프로세스가 죽은 경우도 아래 _conn_misses 에서 똑같이 잡힌다.
            # tasklist 는 시스템 전체 프로세스를 열거하는 무거운 명령인데다, 3초마다 새 프로세스를
            # 띄우는 것 자체가 백신 실시간 감시 훅에 걸려 게임 프레임에 스터터를 유발한다.
            # (배포 exe 에는 psutil 이 포함되지 않아 항상 tasklist 폴백을 탔다.)
            try:
                conn = await self.loop.run_in_executor(None, pz_connected)
            except Exception:
                conn = True                        # 오류 시 오탐 방지로 연결 유지
            if conn:
                self._conn_misses = 0
            else:
                self._conn_misses += 1             # 일시적 오탐 방지로 2회 연속 미감지 시 복귀
                if self._conn_misses >= 2:
                    self._polling = False
                    self.pz_lost.emit()
                    break
            await asyncio.sleep(3)

    def shutdown(self):
        self._polling = False
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)


class LauncherWindow(QWidget):
    def __init__(self, preset=None, auto_advance=True):
        super().__init__()
        self.setWindowTitle("PongDu Launcher  "+VERSION)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.resize(620, 340)
        self.setFixedSize(620, 340)
        self.core = LauncherCore()
        self.core.resolved.connect(self._on_resolved)
        self.core.invalid.connect(self._on_invalid)
        self.core.login_needed.connect(self._on_login_needed)
        self.core.live.connect(self._on_live)
        self.core.connected.connect(self._on_connected)
        self.core.tiers.connect(self._on_tiers)
        self._uuid = ""; self._name = ""
        self._live = False; self._connected = False; self._tier = False
        self._logging_in = False
        self._confirm_ready = False   # 로그인 확인됨 → 로그인 버튼이 '확인' 버튼으로 바뀐 상태
        # 자동 연동시작: 체크리스트 4줄이 전부 초록이 되면 '연동 시작'을 누른 것과 동일하게
        # 메인으로 넘어간다. 최초 실행은 물론이고 PZ 종료 / 로그인 만료 / 미등재로 게이트에
        # 되돌아온 경우도 포함된다 — 스트리머가 게임 안에 있어 런처를 못 보는 상황에서
        # 재접속만으로 연동이 살아나야 하기 때문.
        # 유일한 예외는 메인화면에서 '중지'를 직접 누른 경우로, MainWindow._back_to_gate(
        # manual=True)가 auto_advance=False 를 넘겨 수동 클릭을 기다리게 한다.
        self._auto_advance = bool(auto_advance)
        self._auto_go_done = False    # 같은 창에서 자동 전환은 최초 1회만
        self.cfg = load_config()       # MainWindow와 같은 config.json 공유
        self._game_dir = pz_game_dir() # PZ 설치 폴더 (최적화용, 수동지정 우선 / 못 찾으면 None)
        self._build()
        self.setStyleSheet(DARK_QSS)
        # preset 있으면 로그인 완료 상태(체크리스트)부터 시작, 없으면 자동 로그인 시도
        if preset and preset.get("uuid"):
            self._uuid = preset["uuid"]
            self._name = preset.get("name", "") or (preset["uuid"][:8] + "…")
            self._enter_check_page()
        else:
            QTimer.singleShot(200, self._start_auto_login)
        # 최적화는 자동 적용하지 않는다. 상태 라벨만 갱신하고, 실제 변경은
        # 사용자가 '최적화 설정' 창에서 [적용]을 눌렀을 때만 일어난다.
        self._opt_refresh_ui()

    # --- 빌드 ---
    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18); root.setSpacing(10)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_input())     # 0
        self.stack.addWidget(self._page_invalid())   # 1
        self.stack.addWidget(self._page_check())      # 2
        root.addWidget(self.stack, 1)

    def _muted(self, t):
        l = QLabel(t); l.setObjectName("muted"); return l

    def _sect(self, t):
        l = QLabel(t); l.setObjectName("sect"); return l

    def _page_input(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(10)
        v.addSpacing(45);
        v.addWidget(self._sect("치지직 로그인"))
        self.login_hint = self._muted("연동할 치지직 계정으로 로그인하세요")
        v.addWidget(self.login_hint)
        self.login_status = QLabel(""); self.login_status.setObjectName("muted")
        self.login_status.setAlignment(Qt.AlignCenter)
        v.addWidget(self.login_status)
        row = QHBoxLayout(); row.addStretch(1)
        self.login_btn = QPushButton("치지직 로그인"); self.login_btn.setObjectName("verify")
        self.login_btn.setEnabled(False)             # 자동 로그인 시도가 끝나면 활성화
        self.login_btn.clicked.connect(self._login_click)
        row.addWidget(self.login_btn)
        self.login_cancel_btn = QPushButton("취소")
        self.login_cancel_btn.clicked.connect(self._login_cancel_click)
        self.login_cancel_btn.hide()
        row.addWidget(self.login_cancel_btn)
        row.addStretch(1)
        v.addSpacing(8); v.addLayout(row); v.addStretch(1)
        # 게임 최적화 상태 (하단 고정) — 상태 텍스트 + 설정 창 열기 버튼
        orow = QHBoxLayout(); orow.addStretch(1)
        self.opt_status = QLabel(""); self.opt_status.setObjectName("hint")
        orow.addWidget(self.opt_status)
        self.opt_btn = QPushButton("최적화 설정"); self.opt_btn.setObjectName("link")
        self.opt_btn.clicked.connect(self._open_opt_dialog)
        orow.addWidget(self.opt_btn); orow.addStretch(1)
        v.addLayout(orow)
        return w

    def _page_invalid(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(14)
        v.addWidget(self._sect("치지직 로그인"))
        v.addStretch(1)
        e = QLabel("시즌 참가 목록에 없는 채널입니다"); e.setObjectName("err"); e.setAlignment(Qt.AlignCenter)
        v.addWidget(e)
        h = self._muted("로그인한 계정의 채널이 화이트리스트에 등록돼 있어야 합니다")
        h.setAlignment(Qt.AlignCenter)
        v.addWidget(h)
        row = QHBoxLayout(); row.addStretch(1)
        again = QPushButton("다른 계정으로 로그인"); again.clicked.connect(self._retry)
        row.addWidget(again); row.addStretch(1)
        v.addLayout(row); v.addStretch(1)
        return w

    def _page_check(self):
        w = QWidget(); v = QVBoxLayout(w); v.setContentsMargins(0, 8, 0, 0); v.setSpacing(12)
        v.addWidget(self._sect("연동 준비 확인"))
        self.welcome = QLabel(""); self.welcome.setObjectName("welcome")
        self.welcome.setAlignment(Qt.AlignCenter); self.welcome.setTextFormat(Qt.RichText)
        v.addWidget(self.welcome)
        v.addSpacing(4)
        self.r_uuid = self._check_row(); v.addWidget(self.r_uuid[0])
        self.r_live = self._check_row(); v.addWidget(self.r_live[0])
        self.r_conn = self._check_row(); v.addWidget(self.r_conn[0])
        self.r_tier = self._check_row(); v.addWidget(self.r_tier[0])
        v.addSpacing(6)

        zrow = QHBoxLayout()
        zrow.addStretch(1)
        zrow.addWidget(self._muted("Zomboid 폴더"))
        self.zdir_input = QLineEdit(str(get_zomboid_dir())); self.zdir_input.setReadOnly(True)
        self.zdir_input.setMinimumWidth(300); self.zdir_input.setMaximumWidth(330)
        zrow.addWidget(self.zdir_input)
        zdir_btn = QPushButton("폴더 선택"); zdir_btn.setObjectName("link")
        zdir_btn.clicked.connect(self._choose_zomboid_dir)
        zrow.addWidget(zdir_btn)
        zrow.addStretch(1)
        v.addLayout(zrow)

        v.addSpacing(4)
        row = QHBoxLayout(); row.addStretch(1)
        back_btn = QPushButton("로그아웃"); back_btn.clicked.connect(self._logout)
        row.addWidget(back_btn)
        self.connect_btn = QPushButton("연동 시작"); self.connect_btn.setObjectName("start")
        self.connect_btn.setEnabled(False)
        self.connect_btn.clicked.connect(self._go_main)
        row.addWidget(self.connect_btn); row.addStretch(1)
        v.addLayout(row); v.addStretch(1)
        return w

    def _choose_zomboid_dir(self):
        """Zomboid 폴더를 수동 지정. pz_status.txt/pongdu_tiers.txt/rewards.txt가
           전부 이 폴더 하위 Lua/에서 상대경로로 파생되므로, 자동탐지가 틀렸을 때
           여기 하나만 바로잡으면 셋 다 한 번에 해결된다."""
        start = str(get_zomboid_dir())
        d = QFileDialog.getExistingDirectory(self, "Zomboid 폴더 선택 (Lua 폴더가 있는 상위 폴더)", start)
        if not d:
            return
        set_zomboid_dir_override(Path(d))
        self.zdir_input.setText(d)

    def _check_row(self):
        w = QWidget(); l = QHBoxLayout(w); l.setContentsMargins(120, 0, 0, 0); l.setSpacing(10)
        dot = QLabel("●"); dot.setStyleSheet("color:#ef9f27; font-size:12px;")
        txt = QLabel("")
        l.addWidget(dot); l.addWidget(txt); l.addStretch(1)
        return w, dot, txt

    @staticmethod
    def _set_row(row, done, text):
        _, dot, txt = row
        dot.setStyleSheet(f"color:{'#5dcaa5' if done else '#ef9f27'}; font-size:12px;")
        txt.setText(text)
        txt.setStyleSheet(f"color:{'#e8e8ea' if done else '#9a9ca3'};")

    # --- PZ 최적화 (상태 표시 + 설정 창 열기. 자동 적용 없음) ---
    def _opt_refresh_ui(self):
        """하단 상태 라벨 갱신. 버튼은 항상 '최적화 설정'으로 고정."""
        if self._game_dir is None:
            self.opt_status.setText("게임 최적화 — PZ 설치 폴더를 못 찾음")
            return
        state, heap = pz_optimize_state(self._game_dir)
        if state == "applied":
            self.opt_status.setText(f"게임 최적화 전체 적용됨 · 힙 {heap:,}MB")
        elif state == "partial":
            self.opt_status.setText(f"게임 최적화 일부 적용됨 · 힙 {heap:,}MB")
        else:
            self.opt_status.setText("게임 최적화 미적용")

    def _open_opt_dialog(self):
        dlg = OptimizeDialog(self, self._game_dir)
        dlg.exec_()
        self._game_dir = dlg.game_dir or pz_game_dir()
        self._opt_refresh_ui()


    # --- 흐름 ---
    def _start_auto_login(self):
        """실행 직후: 저장된 로그인으로 조용히 시도. 실패하면 login_needed 로 버튼이 열린다."""
        self._logging_in = True
        self.login_btn.setEnabled(False)
        self.login_status.setText("저장된 로그인 확인 중…")
        self.core.auto_login()

    def _login_click(self):
        """같은 버튼이 두 역할을 한다: 로그인 대기 상태에서는 '치지직 로그인',
           로그인이 확인된 뒤에는 '확인'(→ 연동 준비 확인 화면으로 진행)."""
        if self._confirm_ready:
            self._enter_check_page()
            return
        self._logging_in = True
        self.login_btn.setEnabled(False)
        self.login_cancel_btn.show()
        self.login_status.setText("브라우저에서 치지직 로그인을 완료해 주세요…")
        self.core.browser_login()

    def _login_cancel_click(self):
        """로그인 진행 중이면 진행 중단, 로그인 확인 대기('확인' 표시) 상태면
           그 계정으로 진행하지 않고 저장 토큰을 지운 뒤 로그인 화면으로 되돌린다.
           (확인 상태에서는 core 쪽 로그인 흐름이 이미 끝나 cancel_login 이 무의미하다)"""
        if self._confirm_ready:
            self._uuid = ""; self._name = ""
            _clear_refresh_token()
            self._on_login_needed("")
            return
        self.core.cancel_login()

    def _on_login_needed(self, detail):
        """자동 로그인 실패/브라우저 로그인 실패·취소 → 로그인 버튼 대기 상태."""
        self._logging_in = False
        self._confirm_ready = False
        self.login_btn.setText("치지직 로그인")
        self.login_btn.setEnabled(True)
        self.login_cancel_btn.hide()
        self.login_status.setText(detail or "")
        self.stack.setCurrentIndex(0)

    def _on_resolved(self, uuid, name):
        """로그인/화이트리스트 통과. 여기서 바로 다음 화면으로 넘기지 않고,
           비활성이던 로그인 버튼을 '확인' 버튼으로 바꿔 사용자가 직접 누르게 한다.
           (자동 전환이면 어떤 계정으로 붙었는지 확인할 틈이 없음)"""
        self._uuid = uuid
        self._name = name or (uuid[:8] + "…")
        self._logging_in = False
        self._confirm_ready = True
        # 취소 버튼은 그대로 남긴다 — 비활성 '치지직 로그인' 버튼만 활성 '확인'으로 바뀐다.
        # (이 상태의 취소 = 이 계정으로 진행 안 함 → 토큰 지우고 로그인 화면으로)
        self.login_cancel_btn.show()
        self.login_status.setText(
            f"<span style='color:#5dcaa5; font-weight:bold'>[ {self._name} ]</span> 계정으로 로그인됐습니다")
        self.login_status.setTextFormat(Qt.RichText)
        self.login_btn.setText("확인")
        self.login_btn.setEnabled(True)
        self.stack.setCurrentIndex(0)

    def _enter_check_page(self):
        """'확인'을 눌렀을 때(또는 메인에서 게이트로 복귀했을 때) 연동 준비 확인 화면으로."""
        self._confirm_ready = False
        self.login_btn.setText("치지직 로그인")
        self.login_status.setText("")
        self.login_status.setTextFormat(Qt.AutoText)
        self.welcome.setText(f"<span style='color:#5dcaa5; font-size:26px; font-weight:900'>[ {self._name} ]</span> 님, 환영합니다")
        self._live = False; self._connected = False; self._tier = False
        self._set_row(self.r_uuid, True,  "치지직 로그인 완료")
        self._set_row(self.r_live, False, "방송 상태 확인 중…")
        self._set_row(self.r_conn, False, "인게임 접속 확인 중…")
        self._set_row(self.r_tier, False, "서버 리워드 설정 확인 중…")
        self.connect_btn.setEnabled(False)
        self.stack.setCurrentIndex(2)
        self.core.start_poll(self._uuid)

    def _on_invalid(self):
        self._logging_in = False
        self._confirm_ready = False
        self.login_btn.setText("치지직 로그인")
        self.login_status.setText("")
        self.login_cancel_btn.hide()
        self.stack.setCurrentIndex(1)

    def _retry(self):
        """화이트리스트 미등재 → 다른 계정 로그인. 저장 토큰을 지우고 로그인 화면으로."""
        _clear_refresh_token()
        self._on_login_needed("")

    def _logout(self):
        """체크리스트에서 로그아웃 — 폴링 중단 + 저장 토큰 삭제 후 로그인 화면으로"""
        self.core.stop_poll()
        _clear_refresh_token()
        self._uuid = ""; self._name = ""
        self._live = False; self._connected = False; self._tier = False
        self._on_login_needed("")

    def _on_live(self, live):
        self._live = live
        self._set_row(self.r_live, live,
                      ("방송 중 (강제 온라인)" if FORCE_ONLINE else "방송 중") if live
                      else "방송이 오프라인 상태입니다")
        self._refresh()

    def _on_connected(self, conn):
        self._connected = conn
        self._set_row(self.r_conn, conn,
                      "인게임 접속 완료" if conn else "인게임에 접속되지 않았습니다")
        if not conn:
            # 접속이 끊기면 티어 상태도 즉시 미확인으로 되돌린다 (core가 같은 타이밍에
            # server_tiers를 비우므로 여기서도 동일하게 반영해 화면이 어긋나지 않게 한다).
            self._set_row(self.r_tier, False, "인게임 접속 후 확인됩니다")
        self._refresh()

    def _on_tiers(self, ok):
        self._tier = ok
        if ok:
            n = len(self.core.server_tiers or {})
            self._set_row(self.r_tier, True, f"서버 리워드 설정 확인됨 ({n}개)")
        else:
            self._set_row(self.r_tier, False,
                          "서버 리워드 설정을 찾을 수 없습니다 (모드 버전 확인 필요)")
        self._refresh()

    def _refresh(self):
        ready = self._live and self._connected and self._tier
        self.connect_btn.setEnabled(ready)
        if ready and self._auto_advance and not self._auto_go_done:
            self._auto_go_done = True   # 중복 트리거 방지 (poll 주기마다 _refresh 호출됨)
            self._go_main()

    def _go_main(self):
        self.core.stop_poll()
        play_connect_sound()
        preset = {
            "uuid": self._uuid, "name": self._name, "autostart": True,
            # 매핑을 여기서 계산하지 않는다 — core가 pongdu_tiers.txt에서 읽어들인
            # 값을 그대로 넘긴다. 연동 시작 버튼은 이 값이 있어야만 눌리므로 None일 수 없다.
            "reward_tiers": dict(self.core.server_tiers or {}),
            "server_name": self.core.server_name,
            "server_ts": self.core.server_ts,
        }
        swap_window(MainWindow(preset=preset), self)

    def closeEvent(self, e):
        try:
            self.core.shutdown()
        except Exception:
            pass
        super().closeEvent(e)


# ── 자동 업데이트 ─────────────────────────────────────────────────────────────
# 동작 원리(Windows):
#   실행 중인 exe 는 "삭제"는 막히지만 "이름 변경"은 허용된다. 이 성질을 이용해
#   배치 스크립트나 별도 부트스트래퍼 없이 자기 자신을 교체한다.
#       1) 새 exe 를 PongDu.exe.new 로 받는다 (같은 폴더 — 볼륨이 달라지면 rename 이 복사가 됨)
#       2) sha256 검증
#       3) PongDu.exe → PongDu.exe.old      (실행 중이어도 성공)
#       4) PongDu.exe.new → PongDu.exe
#       5) 새 exe 를 --updated 로 띄우고 현재 프로세스 종료
#       6) 새 프로세스가 시작할 때 .old 삭제 (그때는 파일 락이 풀려 있다)
#   3~4 사이에서 죽으면 exe 가 사라지므로, 실패 시 즉시 롤백한다.

def parse_version(s):
    """'v5.4.3' → (5, 4, 3). 비교 불가능한 값이면 None."""
    nums = re.findall(r"\d+", str(s or ""))
    if not nums:
        return None
    return tuple(int(n) for n in nums[:4])


def _update_url_ok(url) -> bool:
    """https + 허용 호스트인지. 리다이렉트 최종 URL 도 이걸로 다시 검사한다."""
    from urllib.parse import urlparse
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    return p.scheme == "https" and (p.hostname or "") in UPDATE_ALLOWED_HOSTS


def cleanup_old_exe():
    """이전 업데이트가 남긴 PongDu.exe.old 제거. 실패해도 무시(다음 실행 때 다시 시도)."""
    if not IS_FROZEN:
        return
    old = sys.executable + ".old"
    for _ in range(10):
        try:
            os.remove(old)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.3)   # 직전 프로세스가 아직 완전히 안 죽었을 수 있다


class UpdateWorker(QObject):
    """매니페스트 조회 + exe 다운로드/교체를 백그라운드 스레드에서 수행.

       조회는 런처 부팅을 막지 않는다(게이트는 정상적으로 뜨고, 결과가 오면
       그 위에 다이얼로그가 모달로 올라온다). 조회 실패는 조용히 무시 —
       업데이트 서버가 죽었다고 방송 준비가 막히면 안 되므로."""

    found     = pyqtSignal(dict)        # 새 버전 있음 → 매니페스트 dict
    progress  = pyqtSignal(int, int)    # 받은 바이트, 전체 바이트(모르면 0)
    stage     = pyqtSignal(str)         # 현재 단계 문구 (다운로드 이후 구간용)
    finished  = pyqtSignal(str)         # "" = 성공(재실행 대기), 그 외 = 에러 메시지

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # --- 조회 ---
    def check_async(self):
        threading.Thread(target=self._check, daemon=True).start()

    def _check(self):
        try:
            man = self._fetch_manifest()
        except Exception as e:
            print("[update] check failed: %s: %s" % (type(e).__name__, e))
            return
        cur = parse_version(VERSION)
        new = parse_version(man.get("version"))
        if not cur or not new or new <= cur:
            return
        if not _update_url_ok(man.get("url")) or not man.get("sha256"):
            print("[update] manifest rejected (bad url or missing sha256)")
            return
        floor = parse_version(man.get("min_supported"))
        man["_forced"] = bool(floor and cur < floor)
        self.found.emit(man)

    def _fetch_manifest(self):
        import urllib.request
        req = urllib.request.Request(
            UPDATE_MANIFEST_URL,
            headers={"User-Agent": "PongDuLauncher/" + VERSION,
                     "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as r:
            raw = r.read(64 * 1024)
        return json.loads(raw.decode("utf-8"))

    # --- 적용 ---
    def apply_async(self, man):
        threading.Thread(target=self._apply, args=(man,), daemon=True).start()

    def _apply(self, man):
        if not IS_FROZEN:
            self.finished.emit("개발 모드(python gui.py)에서는 자동 업데이트를 쓸 수 없습니다.")
            return
        exe = sys.executable
        tmp = exe + ".new"
        old = exe + ".old"
        try:
            self._download(man["url"], tmp, man["sha256"])
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            if self._cancel:
                self.finished.emit("취소됨")
            else:
                self.finished.emit("다운로드 실패: %s: %s" % (type(e).__name__, e))
            return

        # 교체 (여기서부터는 짧고, 실패하면 즉시 되돌린다)
        # 다만 백신 실시간 감시가 방금 받은 exe 를 스캔하면서 잠깐 잠그면 os.replace 가
        # 수 초 블로킹될 수 있다. 그동안 "다운로드 중…" 이 그대로 떠 있으면 멈춘 것처럼
        # 보이므로 단계를 명시한다.
        self.stage.emit("설치 중… (백신 검사 중이면 잠시 걸릴 수 있습니다)")
        try:
            try:
                os.remove(old)
            except OSError:
                pass
            os.replace(exe, old)          # 실행 중이어도 rename 은 허용됨
        except OSError as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.finished.emit(
                "기존 파일을 교체할 수 없습니다 (%s).\n"
                "런처를 바탕화면 등 쓰기 가능한 폴더로 옮긴 뒤 다시 시도하세요." % e)
            return
        try:
            os.replace(tmp, exe)
        except OSError as e:
            os.replace(old, exe)          # 롤백 — exe 가 사라진 채로 끝나면 안 된다
            self.finished.emit("교체 실패, 원래 버전으로 되돌렸습니다: %s" % e)
            return

        self.finished.emit("")

    def _download(self, url, dest, want_sha):
        import hashlib
        import urllib.request
        req = urllib.request.Request(
            url, headers={"User-Agent": "PongDuLauncher/" + VERSION})
        h = hashlib.sha256()
        got = 0
        with urllib.request.urlopen(req, timeout=UPDATE_DL_TIMEOUT) as r:
            # 리다이렉트를 따라간 최종 URL 재검증
            final = getattr(r, "url", None) or url
            if not _update_url_ok(final):
                raise ValueError("허용되지 않은 다운로드 위치: %s" % final)
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                total = 0
            self.progress.emit(0, total)
            with open(dest, "wb") as f:
                while True:
                    if self._cancel:
                        raise IOError("취소됨")
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self.progress.emit(got, total)
        if total and got != total:
            raise IOError("전송이 중간에 끊겼습니다 (%d/%d bytes)" % (got, total))
        real = h.hexdigest()
        if real.lower() != str(want_sha).strip().lower():
            raise ValueError("해시 불일치 — 파일이 손상됐거나 변조됐습니다")


class UpdateDialog(QDialog):
    """새 버전 안내 → [지금 업데이트] 시 그 자리에서 받아 교체하고 재실행."""

    def __init__(self, man, parent=None):
        super().__init__(parent)
        self.man = man
        self.forced = bool(man.get("_forced"))
        self.worker = UpdateWorker(self)
        self.worker.progress.connect(self._on_progress)
        self.worker.stage.connect(self._on_stage)
        self.worker.finished.connect(self._on_finished)
        self._working = False
        self.setWindowTitle("퐁듀 런처 업데이트")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            self.setWindowIcon(QIcon(ico))
        self.setFixedWidth(440)
        self._build()
        self.setStyleSheet(DARK_QSS)

    def _build(self):
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 18); v.setSpacing(10)

        title = QLabel("새 버전이 있습니다"); title.setObjectName("sect")
        v.addWidget(title)

        cur_new = QLabel("%s  →  %s" % (VERSION, self.man.get("version", "?")))
        cur_new.setObjectName("linkok")
        v.addWidget(cur_new)

        notes = (self.man.get("notes") or "").strip()
        if notes:
            box = QTextEdit(); box.setReadOnly(True); box.setPlainText(notes)
            box.setFixedHeight(110)
            v.addWidget(box)

        if self.forced:
            warn = QLabel("이 버전은 더 이상 정상 동작하지 않습니다. 업데이트가 필요합니다.")
            warn.setObjectName("err"); warn.setWordWrap(True)
            v.addWidget(warn)

        self.bar = QProgressBar(); self.bar.setRange(0, 100); self.bar.setValue(0)
        self.bar.hide()
        v.addWidget(self.bar)

        self.status = QLabel(""); self.status.setObjectName("muted"); self.status.setWordWrap(True)
        v.addWidget(self.status)

        row = QHBoxLayout(); row.addStretch(1)
        self.later_btn = QPushButton("종료" if self.forced else "나중에")
        self.later_btn.clicked.connect(self._later_click)
        row.addWidget(self.later_btn)
        self.go_btn = QPushButton("지금 업데이트"); self.go_btn.setObjectName("verify")
        self.go_btn.clicked.connect(self._go_click)
        row.addWidget(self.go_btn)
        v.addLayout(row)

    # --- 동작 ---
    def _close_guarded(self) -> bool:
        """닫기 시도를 받아 실제로 닫아도 되는지 판정. 작업 중이면 취소로 전환하고 False."""
        if self._working:
            self.worker.cancel()
            self.status.setText("취소하는 중…")
            return False
        return True

    def closeEvent(self, e):
        """X 버튼 / 시스템 메뉴 '닫기' 경로."""
        if self._close_guarded():
            super().closeEvent(e)
        else:
            e.ignore()

    def reject(self):
        """ESC 키와 '나중에/취소' 버튼이 함께 타는 경로."""
        if not self._close_guarded():
            return
        super().reject()
        if self.forced:
            QApplication.quit()

    def _later_click(self):
        self.reject()   # 작업 중이면 취소로 전환, 아니면 닫기 (강제 버전이면 종료까지)

    def _go_click(self):
        if self._working:
            return
        self._working = True
        self.go_btn.setEnabled(False)
        self.later_btn.setText("취소")
        self.bar.show(); self.bar.setValue(0)
        self.status.setText("다운로드 중…")
        # 작업 중 닫기 차단은 closeEvent/reject 가드가 담당한다.
        # 예전엔 setWindowFlag(WindowCloseButtonHint, False) 로 X 버튼을 없앴는데,
        # 이 호출은 내부적으로 setParent(parent, flags) 를 타면서 네이티브 윈도우 핸들을
        # 파괴·재생성한다. exec_() 모달 루프 안에서 그러면 다이얼로그가 숨은 채로 살아남아
        # (뒤이은 show() 로도 복구 안 됨) 모달만 남아 부모 창이 영구히 잠겼다.
        # 증상: 게이트가 보이는데 어딜 눌러도 경고음만 나고, 창 캡처에도 안 잡힘.
        self.worker.apply_async(self.man)

    def _on_progress(self, got, total):
        if total > 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(got * 100 / total))
            self.status.setText("다운로드 중…  %.1f / %.1f MB"
                                % (got / 1048576.0, total / 1048576.0))
        else:
            self.bar.setRange(0, 0)   # 전체 크기를 모르면 무한 진행바
            self.status.setText("다운로드 중…  %.1f MB" % (got / 1048576.0,))

    def _on_stage(self, text):
        self.bar.setRange(0, 0)      # 남은 시간을 알 수 없는 구간 → 무한 진행바
        self.status.setText(text)
        self.later_btn.setEnabled(False)   # 파일 교체 중엔 취소해도 되돌릴 게 없다

    def _on_finished(self, err):
        self._working = False
        if err:
            self.bar.hide()
            self.bar.setRange(0, 100)
            self.go_btn.setEnabled(True)
            self.later_btn.setEnabled(True)
            self.later_btn.setText("종료" if self.forced else "나중에")
            self.status.setText(err)
            return
        self.bar.setRange(0, 100); self.bar.setValue(100)
        self.status.setText("업데이트 완료 — 런처를 다시 시작합니다.")
        self.later_btn.setEnabled(False)
        QTimer.singleShot(600, self._restart)

    def _restart(self):
        """새 exe 를 띄우고 현재 프로세스를 끝낸다.

           단일 인스턴스 락(QSharedMemory)은 이 프로세스가 죽어야 풀리므로,
           새 프로세스에 --updated 를 넘겨 락 획득을 잠깐 재시도하게 한다."""
        import subprocess
        try:
            flags = 0
            if os.name == "nt":
                flags = 0x00000008 | 0x08000000    # DETACHED_PROCESS | CREATE_NO_WINDOW
            subprocess.Popen([sys.executable, "--updated"],
                             cwd=os.path.dirname(sys.executable) or None,
                             close_fds=True, creationflags=flags)
        except Exception as e:
            QMessageBox.warning(
                self, "재시작 실패",
                "업데이트는 끝났지만 자동 재시작에 실패했습니다 (%s).\n"
                "런처를 직접 다시 실행해주세요." % e)
        self.accept()
        QApplication.quit()
        os._exit(0)      # Qt 이벤트루프/백그라운드 스레드가 종료를 붙잡지 않게 즉시 탈출


def acquire_single_instance(retry: bool):
    """단일 인스턴스 락. retry=True(업데이트 직후 재실행)면 직전 프로세스가
       완전히 죽을 때까지 잠깐 기다린다."""
    mem = QSharedMemory("PuppetChzzkLauncher_SingleInstance")
    if mem.create(1):
        return mem
    if not retry:
        return None
    for _ in range(25):          # 최대 5초
        time.sleep(0.2)
        if mem.create(1):
            return mem
    return None


def main():
    # 승격 헬퍼 진입점 — 단일 인스턴스 락보다 먼저 처리해야 함
    # (본체가 떠 있는 상태에서 관리자 권한으로 재실행되는 프로세스라 락을 잡으면 안 됨)
    if "--pz-optimize" in sys.argv:
        _optimizer_cli(sys.argv)
        return

    # 직전 업데이트가 남긴 .old 정리 (락보다 먼저 — 이 시점엔 구 프로세스가 이미 죽어 있다)
    cleanup_old_exe()

    app = QApplication(sys.argv)

    shared_mem = acquire_single_instance(retry="--updated" in sys.argv)
    if shared_mem is None:
        from PyQt5.QtWidgets import QMessageBox
        QMessageBox.warning(None, "중복 실행", "이미 실행 중입니다.")
        sys.exit(0)

    ico = resource_path(ICON_FILE)
    if os.path.exists(ico):
        app.setWindowIcon(QIcon(ico))
    swap_window(LauncherWindow())   # 최초 창도 _ACTIVE_WINDOW 가 붙들게 한다

    # 업데이트 확인 — 부팅을 막지 않는다. 조회는 백그라운드로 돌고,
    # 새 버전이 있을 때만 게이트 위에 다이얼로그가 모달로 올라온다.
    # 부모는 호출 시점의 현재 창으로 잡는다 — 최초 게이트를 캡처해 두면
    # 그 사이 창이 교체됐을 때 이미 삭제된 객체를 참조하게 된다.
    updater = UpdateWorker(app)
    updater.found.connect(lambda man: UpdateDialog(man, _ACTIVE_WINDOW).exec_())
    if IS_FROZEN:
        QTimer.singleShot(800, updater.check_async)

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
