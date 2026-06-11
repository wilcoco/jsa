from flask import Flask, render_template, request, redirect, url_for, flash, session, Response
from datetime import datetime
import os
import psycopg2
import psycopg2.extras
import smtplib
from email.message import EmailMessage
import csv
import io

# PostgreSQL 연결 문자열 (Railway 에서는 DATABASE_URL 환경변수가 자동 주입된다)
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://localhost/fund_plans")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "0") == "1"
FROM_EMAIL = os.environ.get("FROM_EMAIL", SMTP_USER)


def _translate(sql: str) -> str:
    """sqlite 스타일 SQL 을 PostgreSQL 스타일로 변환한다 (? -> %s, IFNULL -> COALESCE)."""
    return sql.replace("?", "%s").replace("IFNULL", "COALESCE")


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=None):
        self._cur.execute(_translate(sql), params or None)
        return self

    def executemany(self, sql, seq_of_params):
        self._cur.executemany(_translate(sql), seq_of_params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _Connection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        # DictCursor 행은 이름/인덱스 양쪽으로 접근 가능 (sqlite3.Row 와 호환)
        return _Cursor(self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor))

    def commit(self):
        self._conn.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()
        return False


def db_conn() -> _Connection:
    return _Connection(psycopg2.connect(DATABASE_URL))

DEPARTMENT_EMAILS = {
    "개발품질팀": ["wjdtmfdk51@icams.co.kr"],
    "금형개발팀": [],
    "상생협력팀": [],
    "생산기술팀": [],
    "생산팀": [],
}


def get_department_settings() -> dict[str, dict]:
    """DB에 저장된 부서 설정(표시이름/활성/이메일)을 읽어온다.

    반환 형식: { department_key: {"display_name": str|None, "active": bool, "emails": [str, ...]} }
    """

    settings: dict[str, dict] = {}
    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT department, display_name, active, emails FROM department_settings")
        for dept, display_name, active, emails_raw in cursor.fetchall():
            if not emails_raw:
                emails_list: list[str] = []
            else:
                parts = [addr.strip() for addr in emails_raw.split(",")]
                emails_list = [addr for addr in parts if addr]
            settings[dept] = {
                "display_name": display_name,
                "active": bool(active) if active is not None else True,
                "emails": emails_list,
            }
    return settings


def save_department_settings(data: dict[str, dict]) -> None:
    """부서별 설정을 DB에 저장한다.

    data 형식: { department_key: {"display_name": str|None, "active": bool, "emails": [str, ...]} }
    """

    with db_conn() as conn:
        cursor = conn.cursor()
        for dept, info in data.items():
            display_name = info.get("display_name") or None
            active = 1 if info.get("active", True) else 0
            emails = info.get("emails", []) or []
            emails_str = ",".join([addr.strip() for addr in emails if addr.strip()]) or None
            cursor.execute(
                """
                INSERT INTO department_settings (department, display_name, active, emails)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(department) DO UPDATE SET
                    display_name = excluded.display_name,
                    active = excluded.active,
                    emails = excluded.emails
                """,
                (dept, display_name, active, emails_str),
            )
        conn.commit()


def get_department_display_name_map() -> dict[str, str]:
    """부서 키 -> 화면 표시 이름 매핑을 반환한다."""

    settings = get_department_settings()
    mapping: dict[str, str] = {}
    # DB 설정 우선
    for dept, info in settings.items():
        name = info.get("display_name") or dept
        mapping[dept] = name

    # 하드코딩된 부서들도 포함 (display_name 미설정 시 키 그대로 사용)
    for dept in DEPARTMENT_EMAILS.keys():
        mapping.setdefault(dept, dept)

    return mapping


def get_active_departments() -> list[str]:
    """새 계획 등록/알림 발송에 사용할 활성 부서 목록 키를 반환한다."""

    settings = get_department_settings()
    active_depts = {dept for dept, info in settings.items() if info.get("active", True)}

    # DB에 없는 부서들은 기본적으로 active 로 취급
    for dept in DEPARTMENT_EMAILS.keys():
        if dept not in settings:
            active_depts.add(dept)

    # fund_plans 에만 존재하는 부서도 일단 active 로 포함 (과거 데이터용)
    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT department FROM fund_plans")
        for (dept,) in cursor.fetchall():
            if dept and dept not in settings:
                active_depts.add(dept)

    return sorted(active_depts)


def get_departments() -> list[str]:
    """전체 부서 키 목록을 반환한다 (활성/비활성 모두 포함)."""

    settings = get_department_settings()
    dept_keys = set(settings.keys())

    # 하드코딩된 부서
    dept_keys.update(DEPARTMENT_EMAILS.keys())

    # 실제 데이터에 존재하는 부서
    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT department FROM fund_plans")
        for (dept,) in cursor.fetchall():
            if dept:
                dept_keys.add(dept)

    return sorted(dept_keys)


def get_month_deadline(department: str, year_month: str) -> dict | None:
    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT department, year_month, register_deadline, check_deadline
            FROM month_deadlines
            WHERE department = ? AND year_month = ?
            """,
            (department, year_month),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)


def save_month_deadline(
    department: str,
    year_month: str,
    register_deadline: str | None,
    check_deadline: str | None,
) -> None:
    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO month_deadlines (department, year_month, register_deadline, check_deadline)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(department, year_month) DO UPDATE SET
                register_deadline = excluded.register_deadline,
                check_deadline = excluded.check_deadline
            """,
            (department, year_month, register_deadline, check_deadline),
        )
        conn.commit()


def can_edit_plan_by_deadline(department: str, year_month: str) -> bool:
    info = get_month_deadline(department, year_month)
    if not info:
        return True
    register_deadline = info.get("register_deadline")
    if not register_deadline:
        return True
    try:
        today = datetime.today().strftime("%Y-%m-%d")
        return today <= register_deadline
    except Exception:  # noqa: BLE001
        return True


def get_department_email_settings() -> dict[str, list[str]]:
    """부서별 메일 수신자 목록만 단순하게 반환한다.

    반환 형식: { department_key: [email1, email2, ...] }
    """

    settings = get_department_settings()
    email_map: dict[str, list[str]] = {}

    for dept, info in settings.items():
        emails = info.get("emails", []) or []
        email_map[dept] = emails

    # DB 에 없는 부서는 기본 DEPARTMENT_EMAILS 값을 사용
    for dept, emails in DEPARTMENT_EMAILS.items():
        if dept not in email_map:
            email_map[dept] = emails

    return email_map


def can_check_or_carry_by_deadline(department: str, year_month: str) -> bool:
    info = get_month_deadline(department, year_month)
    if not info:
        return True
    check_deadline = info.get("check_deadline")
    if not check_deadline:
        return True
    try:
        today = datetime.today().strftime("%Y-%m-%d")
        return today <= check_deadline
    except Exception:  # noqa: BLE001
        return True


def init_db() -> None:
    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS fund_plans (
                id SERIAL PRIMARY KEY,
                department TEXT NOT NULL,
                year_month TEXT NOT NULL,
                amount BIGINT NOT NULL,
                deadline TEXT NOT NULL,
                vendor_name TEXT,
                description TEXT,
                contract_amount BIGINT,
                registered_flag INTEGER DEFAULT 0,
                carry_over INTEGER DEFAULT 0,
                status TEXT DEFAULT 'draft',
                created_at TEXT NOT NULL
            )
            """
        )
        # 부서별 설정(현재는 메일 수신자 목록)을 저장하는 테이블 생성
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS department_settings (
                department TEXT PRIMARY KEY,
                display_name TEXT,
                active INTEGER DEFAULT 1,
                emails TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS month_deadlines (
                department TEXT NOT NULL,
                year_month TEXT NOT NULL,
                register_deadline TEXT,
                check_deadline TEXT,
                PRIMARY KEY (department, year_month)
            )
            """
        )
        conn.commit()


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")

init_db()


def get_all_plans():
    """기존 개별 계획 전체 조회 (현재는 상세 화면에서 사용 예정)."""
    is_closed = False

    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, department, year_month, amount, deadline, vendor_name, description, contract_amount, registered_flag, carry_over, created_at
            FROM fund_plans
            ORDER BY year_month DESC, department ASC, id DESC
            """
        )
        return cursor.fetchall()


def get_department_summaries(selected_department: str | None = None):
    """부서+계획월 기준 누적 금액 및 상태 요약 조회.

    selected_department 가 주어지면 해당 부서만 필터링한다.
    """

    with db_conn() as conn:
        cursor = conn.cursor()

        base_sql = """
            SELECT
                department,
                year_month,
                SUM(amount) AS total_amount,
                MAX(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS has_closed,
                MAX(CASE WHEN status = 'draft' THEN 1 ELSE 0 END) AS has_draft,
                MAX(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) AS has_submitted,
                MAX(
                    CASE
                        WHEN registered_flag = 1 OR carry_over = 1 THEN 1
                        ELSE 0
                    END
                ) AS has_checked
            FROM fund_plans
        """

        params: list[object] = []
        if selected_department:
            base_sql += "\n            WHERE department = ?\n"
            params.append(selected_department)

        base_sql += """
            GROUP BY department, year_month
            ORDER BY year_month DESC, department ASC
        """

        cursor.execute(base_sql, params)
        rows = cursor.fetchall()

    summaries: list[dict] = []
    for row in rows:
        d = dict(row)
        if d.get("has_closed"):
            d["status_label"] = "마감"
        elif d.get("has_checked"):
            d["status_label"] = "체크완료"
        elif d.get("has_submitted"):
            d["status_label"] = "제출완료"
        elif d.get("has_draft"):
            d["status_label"] = "임시저장"
        else:
            d["status_label"] = "미등록"
        summaries.append(d)

    return summaries


@app.route("/")
def index():
    selected_department = request.args.get("department_filter", "").strip() or None

    department_summaries = get_department_summaries(selected_department)
    today = datetime.today().strftime("%Y-%m-%d")
    # 알림/관리자용 부서 선택: 활성 부서
    departments = get_active_departments()
    name_map = get_department_display_name_map()
    # 요약 테이블에서 사용할 표시 이름을 함께 전달
    decorated_summaries = []
    for row in department_summaries:
        d = dict(row)
        dept_key = d.get("department")
        d["department_display"] = name_map.get(dept_key, dept_key)
        decorated_summaries.append(d)
    filter_options = [(dept, name_map.get(dept, dept)) for dept in departments]
    return render_template(
        "index.html",
        department_summaries=decorated_summaries,
        today=today,
        departments=departments,
        department_filter_options=filter_options,
        selected_department_filter=selected_department,
    )


@app.route("/departments/<department>/<year_month>")
def department_detail(department: str, year_month: str):
    """특정 부서+계획 월의 세부 자금계획 목록을 보여주는 화면."""
    # year_month 기준으로 전월/다음월 계산 (YYYY-MM 형식)
    prev_year_month = None
    next_year_month = None
    try:
        base_dt = datetime.strptime(year_month + "-01", "%Y-%m-%d")
        # 전월: day=1에서 month-1
        if base_dt.month == 1:
            prev_year = base_dt.year - 1
            prev_month = 12
        else:
            prev_year = base_dt.year
            prev_month = base_dt.month - 1
        prev_year_month = f"{prev_year:04d}-{prev_month:02d}"

        # 다음월: month+1
        if base_dt.month == 12:
            next_year = base_dt.year + 1
            next_month = 1
        else:
            next_year = base_dt.year
            next_month = base_dt.month + 1
        next_year_month = f"{next_year:04d}-{next_month:02d}"
    except Exception:  # noqa: BLE001
        # year_month 포맷이 예상과 다를 경우 버튼을 숨기기 위해 None 유지
        prev_year_month = None
        next_year_month = None

    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, department, year_month, amount, deadline, vendor_name, description, contract_amount, registered_flag, carry_over, created_at
            FROM fund_plans
            WHERE department = ? AND year_month = ?
            ORDER BY deadline ASC, id DESC
            """,
            (department, year_month),
        )
        plans = cursor.fetchall()

        # 같은 계약(업체명+내용+계약금액) 기준으로 "실제 지급된 금액" 합계를 구해 남은 잔액 계산
        cursor.execute(
            """
            SELECT vendor_name, description, contract_amount, SUM(amount) AS total_paid
            FROM fund_plans
            WHERE department = ?
              AND registered_flag = 1
              AND vendor_name IS NOT NULL
              AND description IS NOT NULL
              AND contract_amount IS NOT NULL
            GROUP BY vendor_name, description, contract_amount
            """,
            (department,),
        )
        total_rows = cursor.fetchall()

        # 마감 여부 확인
        cursor.execute(
            """
            SELECT MAX(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)
            FROM fund_plans
            WHERE department = ? AND year_month = ?
            """,
            (department, year_month),
        )
        row = cursor.fetchone()
        is_closed = bool(row[0]) if row and row[0] is not None else False

        totals_by_contract = {}
        for row in total_rows:
            key = (row["vendor_name"], row["description"], row["contract_amount"])
            totals_by_contract[key] = row["total_paid"] or 0

    enriched_plans = []
    for row in plans:
        data = dict(row)
        contract_amount = data.get("contract_amount")
        total_paid = None
        remaining = None
        if contract_amount is not None:
            key = (data.get("vendor_name"), data.get("description"), contract_amount)
            total_paid = totals_by_contract.get(key, 0)
            remaining = contract_amount - total_paid
        data["total_paid"] = total_paid
        data["remaining"] = remaining
        enriched_plans.append(data)

    total_contract_amount = 0
    total_amount = 0
    total_total_paid = 0
    total_remaining = 0

    for p in enriched_plans:
        if p.get("contract_amount") is not None:
            total_contract_amount += p.get("contract_amount") or 0
        total_amount += p.get("amount") or 0
        if p.get("total_paid") is not None:
            total_total_paid += p.get("total_paid") or 0
        if p.get("remaining") is not None:
            total_remaining += p.get("remaining") or 0

    today = datetime.today().strftime("%Y-%m-%d")

    # 월별 등록/체크 마감 정보 조회
    deadline_info = get_month_deadline(department, year_month) or {}
    register_deadline = deadline_info.get("register_deadline")
    check_deadline = deadline_info.get("check_deadline")
    is_register_deadline_over = False
    is_check_deadline_over = False
    if register_deadline:
        try:
            is_register_deadline_over = today > register_deadline
        except Exception:  # noqa: BLE001
            is_register_deadline_over = False
    if check_deadline:
        try:
            is_check_deadline_over = today > check_deadline
        except Exception:  # noqa: BLE001
            is_check_deadline_over = False
    return render_template(
        "department_detail.html",
        department=department,
        year_month=year_month,
        plans=enriched_plans,
        today=today,
        prev_year_month=prev_year_month,
        next_year_month=next_year_month,
        is_closed=is_closed,
        total_contract_amount=total_contract_amount,
        total_amount=total_amount,
        total_total_paid=total_total_paid,
        total_remaining=total_remaining,
        register_deadline=register_deadline,
        check_deadline=check_deadline,
        is_register_deadline_over=is_register_deadline_over,
        is_check_deadline_over=is_check_deadline_over,
    )


@app.route("/departments/<department>/<year_month>/export")
def export_department_plans(department: str, year_month: str):
    """특정 부서의 자금계획을 엑셀(CSV)로 다운로드한다.

    - start_ym/end_ym 쿼리 파라미터가 없는 경우: 기존처럼 특정 월(year_month) 데이터만 다운로드
    - start_ym/end_ym 이 있는 경우: 선택한 기간에 대한 월별 피벗 형태로 다운로드
    """

    start_ym = request.args.get("start_ym", "").strip()
    end_ym = request.args.get("end_ym", "").strip()

    # 기간 선택이 없거나, 시작/종료가 같은 한 달인 경우: 기존 단일 월 내보내기 로직 사용
    if not start_ym or not end_ym or start_ym == end_ym:
        # start_ym 이 있으면 그것을 우선 사용하고, 없으면 URL 의 year_month 사용
        target_ym = start_ym or year_month
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, department, year_month, amount, deadline, vendor_name, description, contract_amount, registered_flag, carry_over, created_at
                FROM fund_plans
                WHERE department = ? AND year_month = ?
                ORDER BY deadline ASC, id DESC
                """,
                (department, target_ym),
            )
            plans = cursor.fetchall()

            cursor.execute(
                """
                SELECT vendor_name, description, contract_amount, SUM(amount) AS total_paid
                FROM fund_plans
                WHERE department = ?
                  AND registered_flag = 1
                  AND vendor_name IS NOT NULL
                  AND description IS NOT NULL
                  AND contract_amount IS NOT NULL
                GROUP BY vendor_name, description, contract_amount
                """,
                (department,),
            )
            total_rows = cursor.fetchall()

        totals_by_contract: dict[tuple[str, str, int], int] = {}
        for row in total_rows:
            key = (row["vendor_name"], row["description"], row["contract_amount"])
            totals_by_contract[key] = row["total_paid"] or 0

        enriched_plans: list[dict] = []
        for row in plans:
            data = dict(row)
            contract_amount = data.get("contract_amount")
            total_paid = None
            remaining = None
            if contract_amount is not None:
                key = (data.get("vendor_name"), data.get("description"), contract_amount)
                total_paid = totals_by_contract.get(key, 0)
                remaining = contract_amount - total_paid
            data["total_paid"] = total_paid
            data["remaining"] = remaining
            enriched_plans.append(data)

        total_contract_amount = 0
        total_amount = 0
        total_total_paid = 0
        total_remaining = 0

        for p in enriched_plans:
            if p.get("contract_amount") is not None:
                total_contract_amount += p.get("contract_amount") or 0
            total_amount += p.get("amount") or 0
            if p.get("total_paid") is not None:
                total_total_paid += p.get("total_paid") or 0
            if p.get("remaining") is not None:
                total_remaining += p.get("remaining") or 0

        output = io.StringIO()
        writer = csv.writer(output)

        # 화면의 세부내역 테이블 컬럼 순서에 맞추되, 부서명을 맨 앞에 추가한다.
        # 부서, 계획 월, 업체명, 계약금액, 계획금액, 내용, 등록여부, 이월, 누적지급금액, 남은잔액, 등록일시
        header = [
            "부서",
            "계획월",
            "업체명",
            "계약금액",
            "계획금액",
            "내용",
            "등록여부",
            "이월",
            "누적지급금액",
            "남은잔액",
            "등록일시",
        ]
        writer.writerow(header)

        for p in enriched_plans:
            # year_month는 'YYYY-MM' 이지만, 엑셀이 날짜로 바꾸지 않도록 앞에 작은따옴표를 붙여 텍스트로 강제한다.
            raw_year_month = p.get("year_month") or ""
            year_month_text = f"'{raw_year_month}"  # 엑셀에서 2026-01 그대로 보이게

            registered_text = "등록" if p.get("registered_flag") else "-"
            carry_over_text = "이월" if p.get("carry_over") else "-"

            writer.writerow(
                [
                    p.get("department"),  # 부서
                    year_month_text,  # 계획월 (예: 2026-01, 텍스트로 표시)
                    p.get("vendor_name"),  # 업체명
                    p.get("contract_amount"),  # 계약금액
                    p.get("amount"),  # 계획금액
                    p.get("description"),  # 내용
                    registered_text,  # 등록여부 (텍스트)
                    carry_over_text,  # 이월 (텍스트)
                    p.get("total_paid"),  # 누적지급금액
                    p.get("remaining"),  # 남은잔액
                    p.get("created_at"),  # 등록일시
                ]
            )

        writer.writerow(
            [
                "",  # 부서 (합계 행은 비워둔다)
                "",  # 계획월
                "소계",  # 업체명 대신 소계 라벨
                total_contract_amount,
                total_amount,
                "",  # 내용
                "",  # 등록여부
                "",  # 이월
                total_total_paid,
                total_remaining,
                "",  # 등록일시
            ]
        )

        csv_data = output.getvalue().encode("utf-8-sig")
        # HTTP 헤더 인코딩 문제를 피하기 위해 파일명은 ASCII 문자만 사용한다.
        safe_department = "dept"
        filename = f"{target_ym}_{safe_department}_plans.csv"
        response = Response(csv_data, mimetype="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={filename}"
        return response

    # 여기서부터는 기간 선택(start_ym, end_ym) 기반 엑셀 내보내기
    try:
        start_dt = datetime.strptime(start_ym + "-01", "%Y-%m-%d")
        end_dt = datetime.strptime(end_ym + "-01", "%Y-%m-%d")
    except Exception:  # noqa: BLE001
        flash("기간 형식이 올바르지 않습니다.", "error")
        return redirect(url_for("department_detail", department=department, year_month=year_month))

    if start_dt > end_dt:
        flash("시작월이 종료월보다 이후입니다.", "error")
        return redirect(url_for("department_detail", department=department, year_month=year_month))

    # 선택한 기간에 포함되는 year_month 리스트 생성
    months: list[str] = []
    cursor_dt = start_dt
    while cursor_dt <= end_dt:
        months.append(cursor_dt.strftime("%Y-%m"))
        # 다음 달로 이동
        if cursor_dt.month == 12:
            cursor_dt = cursor_dt.replace(year=cursor_dt.year + 1, month=1)
        else:
            cursor_dt = cursor_dt.replace(month=cursor_dt.month + 1)

    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                vendor_name,
                description,
                contract_amount,
                year_month,
                SUM(amount) AS total_amount
            FROM fund_plans
            WHERE department = ?
              AND year_month >= ?
              AND year_month <= ?
            GROUP BY vendor_name, description, contract_amount, year_month
            ORDER BY vendor_name ASC, description ASC, contract_amount ASC, year_month ASC
            """,
            (department, months[0], months[-1]),
        )
        rows = cursor.fetchall()

    # (업체명, 내용, 계약금액) 별로 월별 금액을 모은다.
    by_key: dict[tuple, dict[str, int]] = {}
    base_info: dict[tuple, tuple] = {}
    for row in rows:
        key = (row["vendor_name"], row["description"], row["contract_amount"])
        ym = row["year_month"]
        amount = row["total_amount"] or 0
        if key not in by_key:
            by_key[key] = {}
            base_info[key] = (
                row["vendor_name"],
                row["description"],
                row["contract_amount"],
            )
        by_key[key][ym] = by_key[key].get(ym, 0) + amount

    output = io.StringIO()
    writer = csv.writer(output)

    # 첫 행: 부서명 / 표시이름
    name_map = get_department_display_name_map()
    dept_display = name_map.get(department, department)
    writer.writerow(["부서명", dept_display])

    # 두 번째 행: 컬럼 헤더
    month_headers = []
    for ym in months:
        # 컬럼 라벨은 M월 형태로 표시 (예: 1월, 2월 ...)
        try:
            dt = datetime.strptime(ym + "-01", "%Y-%m-%d")
            label = f"{dt.month}월"
        except Exception:  # noqa: BLE001
            label = ym
        month_headers.append(label)

    header = ["업체명", "내용", "계약금액"] + month_headers
    writer.writerow(header)

    # 데이터 행
    for key, info in base_info.items():
        vendor_name, description, contract_amount = info
        month_values: list[int | str] = []
        month_amounts = by_key.get(key, {})
        for ym in months:
            val = month_amounts.get(ym)
            month_values.append(val or 0)

        writer.writerow([vendor_name, description, contract_amount] + month_values)

    # 컬럼(월)별 소계 행 추가
    month_totals: list[int] = []
    for ym in months:
        total = 0
        for key in base_info.keys():
            amounts = by_key.get(key, {})
            total += amounts.get(ym, 0) or 0
        month_totals.append(total)

    writer.writerow(["소계", "", ""] + month_totals)

    csv_data = output.getvalue().encode("utf-8-sig")
    safe_department = "dept"
    filename = f"{start_ym}_to_{end_ym}_{safe_department}_plans.csv"
    response = Response(csv_data, mimetype="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return response


@app.route("/departments/<department>/<year_month>/flags", methods=["POST"])
def update_plan_flags(department: str, year_month: str):
    """세부내역 화면에서 등록여부/이월 플래그를 수정한다 (마감된 월은 수정 불가)."""

    if not can_check_or_carry_by_deadline(department, year_month):
        flash("체크 마감일이 지나 등록여부 및 이월을 수정할 수 없습니다.", "error")
        return redirect(url_for("department_detail", department=department, year_month=year_month))

    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT MAX(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)
            FROM fund_plans
            WHERE department = ? AND year_month = ?
            """,
            (department, year_month),
        )
        row = cursor.fetchone()
        is_closed = bool(row[0]) if row and row[0] is not None else False

        if is_closed:
            flash("이미 마감된 계획입니다. 등록여부/이월을 수정할 수 없습니다.", "error")
            return redirect(url_for("department_detail", department=department, year_month=year_month))

        # 현재 월의 해당 부서 계획들을 모두 가져온다.
        cursor.execute(
            """
            SELECT id, amount, vendor_name, description, contract_amount, registered_flag, carry_over
            FROM fund_plans
            WHERE department = ? AND year_month = ?
            """,
            (department, year_month),
        )
        rows = cursor.fetchall()

        for row in rows:
            plan_id = row["id"]
            prev_carry = row["carry_over"] or 0

            new_registered = 1 if request.form.get(f"registered_flag_{plan_id}") == "on" else 0
            new_carry = 1 if request.form.get(f"carry_over_{plan_id}") == "on" else 0

            new_vendor_name = request.form.get(f"vendor_name_{plan_id}", "").strip() or None
            new_description = request.form.get(f"description_{plan_id}", "").strip() or None

            # 값 변경 시 업데이트
            if (
                new_registered != row["registered_flag"]
                or new_carry != prev_carry
                or new_vendor_name != row["vendor_name"]
                or new_description != row["description"]
            ):
                cursor.execute(
                    """
                    UPDATE fund_plans
                    SET registered_flag = ?, carry_over = ?, vendor_name = ?, description = ?
                    WHERE id = ?
                    """,
                    (new_registered, new_carry, new_vendor_name, new_description, plan_id),
                )

        conn.commit()

    flash("등록여부 및 이월 설정이 저장되었습니다.", "success")
    return redirect(url_for("department_detail", department=department, year_month=year_month))


@app.route("/plans/new", methods=["GET", "POST"])
def new_plan():
    if request.method == "POST":
        department = request.form.get("department", "").strip()
        year_month = request.form.get("year_month", "").strip()
        action = request.form.get("action", "save")

        if action == "submit":
            status = "submitted"
        else:
            status = "draft"

        errors = []

        if not department:
            errors.append("부서를 입력해 주세요.")

        if not year_month:
            errors.append("계획 월을 선택해 주세요.")

        rows_to_insert = []

        if department and year_month:
            if not can_edit_plan_by_deadline(department, year_month):
                flash("등록 마감일이 지나 계획을 등록/수정할 수 없습니다.", "error")
                return redirect(url_for("new_plan", department=department, year_month=year_month))

            # 마감된 월에 대해서는 더 이상 수정/등록 불가
            with db_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT MAX(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)
                    FROM fund_plans
                    WHERE department = ? AND year_month = ?
                    """,
                    (department, year_month),
                )
                row = cursor.fetchone()
                is_closed = bool(row[0]) if row and row[0] is not None else False
            if is_closed:
                flash("이미 마감된 계획입니다. 수정하거나 등록할 수 없습니다.", "error")
                return redirect(url_for("new_plan", department=department, year_month=year_month))

        for idx in range(1, 201):
            vendor_name = request.form.get(f"vendor_name_{idx}", "").strip()
            contract_amount_raw = request.form.get(f"contract_amount_{idx}", "").strip()
            amount_raw = request.form.get(f"amount_{idx}", "").strip()
            description = request.form.get(f"description_{idx}", "").strip()
            registered_flag_raw = request.form.get(f"registered_flag_{idx}")
            carry_over_raw = request.form.get(f"carry_over_{idx}")

            # 행 전체가 비어 있으면 건너뛴다.
            if (
                not vendor_name
                and not contract_amount_raw
                and not amount_raw
                and not description
            ):
                continue

            row_errors = []

            if not vendor_name:
                row_errors.append(f"{idx}행: 업체명을 입력해 주세요.")

            contract_amount = None
            if contract_amount_raw:
                try:
                    contract_amount = int(contract_amount_raw)
                    if contract_amount <= 0:
                        row_errors.append(f"{idx}행: 계약금액은 0보다 큰 숫자로 입력해 주세요.")
                except ValueError:
                    row_errors.append(f"{idx}행: 계약금액은 숫자로 입력해 주세요.")

            try:
                amount = int(amount_raw)
                if amount <= 0:
                    row_errors.append(f"{idx}행: 계획금액은 0보다 큰 숫자로 입력해 주세요.")
            except ValueError:
                row_errors.append(f"{idx}행: 계획금액은 숫자로 입력해 주세요.")

            if row_errors:
                errors.extend(row_errors)
            else:
                registered_flag = 1 if registered_flag_raw == "on" else 0
                carry_over = 1 if carry_over_raw == "on" else 0

                now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # UI 에서는 개별 행의 기한을 입력받지 않으므로,
                # 계획 월 기준으로 일괄 기본값을 설정한다 (예: YYYY-MM-12)
                deadline_raw = f"{year_month}-12" if year_month else ""

                # 현재 월 계획 행
                rows_to_insert.append(
                    (
                        department,
                        year_month,
                        amount,
                        deadline_raw,
                        vendor_name,
                        description,
                        contract_amount,
                        registered_flag,
                        carry_over,
                        status,
                        now_str,
                    )
                )

                # 이월 여부(carry_over)는 현재 행에만 저장한다.

        if not rows_to_insert and not errors:
            errors.append("입력된 계획이 없습니다. 한 행 이상 내용을 입력해 주세요.")

        if errors:
            for msg in errors:
                flash(msg, "error")
            return redirect(url_for("new_plan"))

        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM fund_plans
                WHERE department = ?
                  AND year_month = ?
                  AND status = 'draft'
                """,
                (department, year_month),
            )
            cursor.executemany(
                """
                INSERT INTO fund_plans (
                    department,
                    year_month,
                    amount,
                    deadline,
                    vendor_name,
                    description,
                    contract_amount,
                    registered_flag,
                    carry_over,
                    status,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )

            conn.commit()

        if status == "submitted":
            flash("자금계획이 최종 제출되었습니다.", "success")
        else:
            flash("자금계획이 임시저장되었습니다.", "success")
        return redirect(url_for("index"))

    today = datetime.today()
    default_year_month = today.strftime("%Y-%m")

    department = request.args.get("department", "").strip()
    selected_year_month = request.args.get("year_month", default_year_month).strip() or default_year_month

    existing_rows: list[dict] = []
    is_closed = False
    register_deadline = None
    check_deadline = None
    is_register_deadline_over = False
    if department and selected_year_month:
        with db_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT vendor_name, contract_amount, amount, description
                FROM fund_plans
                WHERE department = ?
                  AND year_month = ?
                  AND status = 'draft'
                ORDER BY id ASC
                LIMIT 200
                """,
                (department, selected_year_month),
            )
            existing_rows = [dict(row) for row in cursor.fetchall()]

            cursor.execute(
                """
                SELECT MAX(CASE WHEN status = 'closed' THEN 1 ELSE 0 END)
                FROM fund_plans
                WHERE department = ? AND year_month = ?
                """,
                (department, selected_year_month),
            )
            row = cursor.fetchone()
            is_closed = bool(row[0]) if row and row[0] is not None else False

    # 월별 등록 마감 정보 조회 (등록 화면에서는 등록 마감만 사용)
    if department and selected_year_month:
        today_str = today.strftime("%Y-%m-%d")
        info = get_month_deadline(department, selected_year_month) or {}
        register_deadline = info.get("register_deadline")
        check_deadline = info.get("check_deadline")
        if register_deadline:
            try:
                is_register_deadline_over = today_str > register_deadline
            except Exception:  # noqa: BLE001
                is_register_deadline_over = False

    # 이월 표시된 계획 + 잔액이 남아있는 계약의 계획들을 불러와서,
    # 화면에서 선택한 부서/계획 월 기준으로 필터링해 사용한다.
    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                year_month,
                department,
                vendor_name,
                description,
                contract_amount,
                amount,
                CASE
                    WHEN contract_amount IS NOT NULL AND contract_amount > 0 THEN
                        contract_amount - (
                            SELECT IFNULL(SUM(amount), 0)
                            FROM fund_plans AS fp2
                            WHERE
                                fp2.department = fund_plans.department
                                AND fp2.vendor_name = fund_plans.vendor_name
                                AND fp2.description = fund_plans.description
                                AND fp2.contract_amount = fund_plans.contract_amount
                                AND fp2.registered_flag = 1
                        )
                    ELSE amount
                END AS remaining_amount
            FROM fund_plans
            WHERE
                (
                    carry_over = 1
                    OR (
                        contract_amount IS NOT NULL
                        AND contract_amount > 0
                        AND contract_amount > (
                            SELECT IFNULL(SUM(amount), 0)
                            FROM fund_plans AS fp2
                            WHERE
                                fp2.department = fund_plans.department
                                AND fp2.vendor_name = fund_plans.vendor_name
                                AND fp2.description = fund_plans.description
                                AND fp2.contract_amount = fund_plans.contract_amount
                                AND fp2.registered_flag = 1
                        )
                    )
                )
                AND (
                    CASE
                        WHEN contract_amount IS NOT NULL AND contract_amount > 0 THEN
                            contract_amount - (
                                SELECT IFNULL(SUM(amount), 0)
                                FROM fund_plans AS fp2
                                WHERE
                                    fp2.department = fund_plans.department
                                    AND fp2.vendor_name = fund_plans.vendor_name
                                    AND fp2.description = fund_plans.description
                                    AND fp2.contract_amount = fund_plans.contract_amount
                                    AND fp2.registered_flag = 1
                            )
                        ELSE amount
                    END
                ) > 0
            ORDER BY year_month DESC, department ASC, vendor_name ASC, description ASC, id ASC
            """
        )
        # DB row 객체는 바로 JSON 으로 직렬화할 수 없으므로, dict 로 변환해 템플릿에 전달한다.
        # SUM() 결과는 Decimal 로 반환되므로 JSON 직렬화를 위해 int 로 변환한다.
        carry_over_plans = []
        for row in cursor.fetchall():
            d = dict(row)
            if d.get("remaining_amount") is not None:
                d["remaining_amount"] = int(d["remaining_amount"])
            carry_over_plans.append(d)

    # 부서 선택용 옵션: 활성 부서 + 표시 이름
    active_depts = get_active_departments()
    name_map = get_department_display_name_map()
    department_options = [(dept, name_map.get(dept, dept)) for dept in active_depts]

    return render_template(
        "new_plan.html",
        department=department,
        year_month=selected_year_month,
        existing_rows=existing_rows,
        is_closed=is_closed,
        carry_over_plans=carry_over_plans,
        department_options=department_options,
        register_deadline=register_deadline,
        check_deadline=check_deadline,
        is_register_deadline_over=is_register_deadline_over,
    )


@app.route("/departments/<department>/<year_month>/close", methods=["POST"])
def close_plan_month(department: str, year_month: str):
    """관리자 전용: 특정 부서/계획 월 자금계획을 마감 상태로 변경한다."""

    if not session.get("is_admin"):
        flash("관리자 권한이 필요합니다.", "error")
        return redirect(url_for("index"))

    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE fund_plans
            SET status = 'closed'
            WHERE department = ? AND year_month = ?
            """,
            (department, year_month),
        )
        conn.commit()

    flash(f"{department} / {year_month} 자금계획이 마감되었습니다.", "success")
    return redirect(url_for("index"))


@app.route("/departments/<department>/<year_month>/reopen", methods=["POST"])
def reopen_plan_month(department: str, year_month: str):
    """관리자 전용: 특정 부서/계획 월 자금계획의 마감을 해제한다."""

    if not session.get("is_admin"):
        flash("관리자 권한이 필요합니다.", "error")
        return redirect(url_for("index"))

    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE fund_plans
            SET status = 'submitted'
            WHERE department = ? AND year_month = ? AND status = 'closed'
            """,
            (department, year_month),
        )
        conn.commit()

    flash(f"{department} / {year_month} 자금계획 마감이 해제되었습니다.", "success")
    return redirect(url_for("index"))


@app.route("/admin/departments", methods=["GET", "POST"])
def admin_departments():
    if not session.get("is_admin"):
        flash("관리자 권한이 필요합니다.", "error")
        return redirect(url_for("index"))

    departments = get_departments()

    if request.method == "POST":
        # 기존 부서 설정 업데이트
        settings: dict[str, dict] = {}
        for dept in departments:
            display_name = request.form.get(f"display_name_{dept}", "").strip() or None
            active_flag = request.form.get(f"active_{dept}") == "on"
            emails_raw = request.form.get(f"emails_{dept}", "").strip()
            if emails_raw:
                parts = [addr.strip() for addr in emails_raw.split(",")]
                emails = [addr for addr in parts if addr]
            else:
                emails = []

            settings[dept] = {
                "display_name": display_name,
                "active": active_flag,
                "emails": emails,
            }

        # 새 부서 추가
        new_key = request.form.get("new_department_key", "").strip()
        new_display_name = request.form.get("new_display_name", "").strip() or None
        new_emails_raw = request.form.get("new_emails", "").strip()
        new_emails: list[str] = []
        if new_emails_raw:
            parts = [addr.strip() for addr in new_emails_raw.split(",")]
            new_emails = [addr for addr in parts if addr]

        if new_key:
            # 새 부서 키가 기존과 중복되지 않도록 보호
            if new_key in settings:
                flash("이미 존재하는 부서 키입니다. 다른 이름을 사용해 주세요.", "error")
            else:
                settings[new_key] = {
                    "display_name": new_display_name or new_key,
                    "active": True,
                    "emails": new_emails,
                }

        save_department_settings(settings)
        flash("부서별 설정이 저장되었습니다.", "success")
        return redirect(url_for("admin_departments"))

    # GET 요청: 화면에 보여줄 현재 설정값 준비
    db_settings = get_department_settings()
    display_names: dict[str, str] = {}
    active_flags: dict[str, bool] = {}
    email_settings_str: dict[str, str] = {}

    for dept in departments:
        info = db_settings.get(dept)
        if info is not None:
            display_names[dept] = info.get("display_name") or dept
            active_flags[dept] = info.get("active", True)
            emails = info.get("emails", [])
        else:
            display_names[dept] = dept
            active_flags[dept] = True
            emails = DEPARTMENT_EMAILS.get(dept, [])
        email_settings_str[dept] = ", ".join(emails)

    return render_template(
        "admin_departments.html",
        departments=departments,
        display_names=display_names,
        active_flags=active_flags,
        email_settings=email_settings_str,
    )


@app.route("/departments/notify", methods=["POST"])
def send_department_notification():
    if not session.get("is_admin"):
        flash("관리자 권한이 필요합니다.", "error")
        return redirect(url_for("index"))

    department = request.form.get("department", "").strip()
    year_month = request.form.get("year_month", "").strip()
    plan_deadline = request.form.get("plan_deadline", "").strip()
    check_deadline = request.form.get("check_deadline", "").strip()

    if not department or not year_month:
        flash("부서와 계획 월을 모두 입력해 주세요.", "error")
        return redirect(url_for("index"))

    # 등록 마감, 체크 마감은 둘 중 하나만 있어도 발송 가능
    if not plan_deadline and not check_deadline:
        flash("자금계획 등록 마감일 또는 체크 마감일 중 하나 이상을 입력해 주세요.", "error")
        return redirect(url_for("index"))

    # DB 에 저장된 부서별 메일 설정을 우선 사용하고, 없으면 코드 상의 기본값을 사용한다.
    db_settings = get_department_email_settings()
    name_map = get_department_display_name_map()

    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        flash("메일 서버 설정이 되어 있지 않습니다. SMTP_HOST / SMTP_USER / SMTP_PASSWORD 환경변수를 확인해 주세요.", "error")
        return redirect(url_for("index"))

    # send_fire_check_mail.py 방식으로 메일 발송
    from email.mime.text import MIMEText

    if department == "ALL":
        # 전체 부서 선택 시 각 부서별로 개별 메일 발송
        active_depts = get_active_departments()
        sent_count = 0
        failed_depts = []
        
        for dept in active_depts:
            # 각 부서의 마감일 저장
            save_month_deadline(dept, year_month, plan_deadline or None, check_deadline or None)
            
            # 각 부서의 수신자 목록 가져오기
            if dept in db_settings:
                recipients = db_settings.get(dept, [])
            else:
                recipients = DEPARTMENT_EMAILS.get(dept, [])
            recipients = [addr for addr in recipients if addr]
            
            if not recipients:
                continue
            
            # 부서별 표시 이름 가져오기
            dept_display = name_map.get(dept, dept)
            
            # 부서별 메일 제목 및 본문 생성
            subject = f"[{dept_display}] {year_month} 자금계획 등록/체크 요청"
            
            # year_month를 "YYYY년 MM월" 형식으로 변환
            try:
                ym_dt = datetime.strptime(year_month, "%Y-%m")
                year_month_kr = f"{ym_dt.year}년 {ym_dt.month}월"
            except:
                year_month_kr = year_month
            
            # 날짜를 "YYYY년 MM월 DD일" 형식으로 변환하는 함수
            def format_date_kr(date_str):
                try:
                    dt = datetime.strptime(date_str, "%Y-%m-%d")
                    return f"{dt.year}년 {dt.month}월 {dt.day}일"
                except:
                    return date_str
            
            body_lines = [
                f"안녕하세요. {dept_display} 자금계획 담당자님.",
                "",
                f"{year_month_kr} 기준",
                "",
            ]

            if plan_deadline:
                plan_deadline_kr = format_date_kr(plan_deadline)
                body_lines.append(
                    f"1. 자금계획 시스템 등록기한은 {plan_deadline_kr}입니다."
                )

            if check_deadline:
                check_deadline_kr = format_date_kr(check_deadline)
                body_lines.append(
                    f"2. 제출된 자금계획의 '등록여부(전표)' 및 '이월' 체크 기한은 {check_deadline_kr}입니다."
                )

            body_lines.extend([
                "",
                "감사합니다.",
            ])
            body = "\n".join(body_lines)
            
            # 부서별 메일 발송
            msg = MIMEText(body, _charset="utf-8")
            msg["Subject"] = subject
            msg["From"] = FROM_EMAIL or SMTP_USER
            msg["To"] = ", ".join(recipients)

            try:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                    server.login(SMTP_USER, SMTP_PASSWORD)
                    server.sendmail(FROM_EMAIL or SMTP_USER, recipients, msg.as_string())
                sent_count += 1
            except Exception as e:  # noqa: BLE001
                failed_depts.append(f"{dept_display}({e})")
        
        if failed_depts:
            flash(f"일부 부서 메일 발송 실패: {', '.join(failed_depts)}", "error")
        if sent_count > 0:
            flash(f"{sent_count}개 부서에 메일 알림이 발송되었습니다.", "success")
        else:
            flash("메일을 발송할 부서가 없습니다.", "error")
        return redirect(url_for("index"))
    else:
        # 개별 부서 선택 시
        if department in db_settings:
            recipients = db_settings.get(department, [])
        else:
            recipients = DEPARTMENT_EMAILS.get(department, [])
        recipients = [addr for addr in recipients if addr]
        
        if not recipients:
            flash("선택한 대상에 설정된 메일 수신자가 없습니다.", "error")
            return redirect(url_for("index"))
        
        save_month_deadline(department, year_month, plan_deadline or None, check_deadline or None)
        
        # 부서 표시 이름 가져오기
        dept_display = name_map.get(department, department)
        
        subject = f"[{dept_display}] {year_month} 자금계획 등록/체크 요청"
        
        # year_month를 "YYYY년 MM월" 형식으로 변환
        try:
            ym_dt = datetime.strptime(year_month, "%Y-%m")
            year_month_kr = f"{ym_dt.year}년 {ym_dt.month}월"
        except:
            year_month_kr = year_month
        
        # 날짜를 "YYYY년 MM월 DD일" 형식으로 변환하는 함수
        def format_date_kr(date_str):
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                return f"{dt.year}년 {dt.month}월 {dt.day}일"
            except:
                return date_str
        
        body_lines = [
            f"안녕하세요. {dept_display} 자금계획 담당자님.",
            "",
            f"{year_month_kr} 기준",
            "",
        ]

        if plan_deadline:
            plan_deadline_kr = format_date_kr(plan_deadline)
            body_lines.append(
                f"1. 자금계획 시스템 등록기한은 {plan_deadline_kr}입니다."
            )

        if check_deadline:
            check_deadline_kr = format_date_kr(check_deadline)
            body_lines.append(
                f"2. 제출된 자금계획의 '등록여부(전표)' 및 '이월' 체크 기한은 {check_deadline_kr}입니다."
            )

        body_lines.extend([
            "",
            "감사합니다.",
        ])
        body = "\n".join(body_lines)
        
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = subject
        msg["From"] = FROM_EMAIL or SMTP_USER
        msg["To"] = ", ".join(recipients)

        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(FROM_EMAIL or SMTP_USER, recipients, msg.as_string())
        except Exception as e:  # noqa: BLE001
            flash(f"메일 발송 중 오류가 발생했습니다: {e}", "error")
            return redirect(url_for("index"))

        flash("부서 메일 알림이 발송되었습니다.", "success")
        return redirect(url_for("index"))


@app.route("/plans/<int:plan_id>/admin-delete", methods=["POST"])
def admin_delete_plan(plan_id: int):
    if not session.get("is_admin"):
        flash("관리자 권한이 필요합니다.", "error")
        return redirect(request.referrer or url_for("index"))

    department = request.args.get("department")
    year_month = request.args.get("year_month")

    with db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM fund_plans WHERE id = ?", (plan_id,))
        conn.commit()

    flash("선택한 자금계획이 삭제되었습니다.", "success")

    if department and year_month:
        return redirect(url_for("department_detail", department=department, year_month=year_month))
    return redirect(url_for("index"))


@app.route("/admin/login", methods=["POST"])
def admin_login():
    password = request.form.get("password", "")
    if password == ADMIN_PASSWORD:
        session["is_admin"] = True
        flash("관리자 모드로 전환되었습니다.", "success")
    else:
        session.pop("is_admin", None)
        flash("관리자 비밀번호가 올바르지 않습니다.", "error")

    next_url = request.referrer or url_for("index")
    return redirect(next_url)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    flash("관리자 모드가 해제되었습니다.", "success")
    next_url = request.referrer or url_for("index")
    return redirect(next_url)


if __name__ == "__main__":
    app.run(debug=True)
