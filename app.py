import os
import re
import base64
import sqlite3
from datetime import datetime

import streamlit as st
# ============================================================
# GOOGLE LOGIN
# ============================================================

if not st.user.is_logged_in:
    st.title("📧 AI Email Management Agent")
    st.write("Please sign in with your Google account to continue.")

    if st.button("🔐 Sign in with Google"):
        st.login()

    st.stop()

st.sidebar.success(f"Logged in as: {st.user.email}")

if st.sidebar.button("🚪 Logout"):
    st.logout()
import json
from google import genai
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from email.mime.text import MIMEText

from langgraph.graph import StateGraph, START, END


# ============================================================
# PAGE CONFIGURATION
# ============================================================

st.set_page_config(
    page_title="AI Email Management Agent",
    page_icon="📧",
    layout="wide"
)


# ============================================================
# CONFIGURATION
# ============================================================

# Gmail permission
# gmail.modify allows sending replies and other Gmail actions.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify"
]

DB_NAME = "email_agent.db"

GEMINI_MODEL = "gemini-3.8-flash"


# ============================================================
# SESSION STATE
# ============================================================

if "activity_history" not in st.session_state:
    st.session_state.activity_history = []

if "email_cache" not in st.session_state:
    st.session_state.email_cache = []

if "analysis_results" not in st.session_state:
    st.session_state.analysis_results = {}

if "summary_results" not in st.session_state:
    st.session_state.summary_results = {}

if "reply_results" not in st.session_state:
    st.session_state.reply_results = {}

if "agent_results" not in st.session_state:
    st.session_state.agent_results = {}

if "gmail_connected" not in st.session_state:
    st.session_state.gmail_connected = False


# ============================================================
# DATABASE
# ============================================================

def get_db_connection():
    conn = sqlite3.connect(
        DB_NAME,
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    return conn


def initialize_database():

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email_id TEXT UNIQUE,
            subject TEXT,
            task TEXT,
            deadline TEXT,
            sender TEXT,
            completed INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS activity_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT,
            details TEXT,
            created_at TEXT
        )
    """)

    conn.commit()
    conn.close()


initialize_database()


# ============================================================
# ACTIVITY HISTORY
# ============================================================

def add_activity(action, details):

    st.session_state.activity_history.insert(
        0,
        {
            "action": action,
            "details": details
        }
    )

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO activity_history
        (action, details, created_at)
        VALUES (?, ?, ?)
    """, (
        action,
        details,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))

    conn.commit()
    conn.close()


def load_activity_history():

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT action, details, created_at
        FROM activity_history
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()

    conn.close()

    return [dict(row) for row in rows]


def clear_activity_history():

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM activity_history")

    conn.commit()
    conn.close()

    st.session_state.activity_history = []


# Load history when app starts
if not st.session_state.activity_history:
    st.session_state.activity_history = load_activity_history()


# ============================================================
# TASK DATABASE FUNCTIONS
# ============================================================

def save_task_to_database(task):

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id
        FROM tasks
        WHERE email_id = ?
    """, (task["id"],))

    existing = cursor.fetchone()

    if existing is None:

        cursor.execute("""
            INSERT INTO tasks
            (
                email_id,
                subject,
                task,
                deadline,
                sender,
                completed
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            task["id"],
            task["subject"],
            task["task"],
            task["deadline"],
            task["sender"],
            0
        ))

    else:

        cursor.execute("""
            UPDATE tasks
            SET subject = ?,
                task = ?,
                deadline = ?,
                sender = ?
            WHERE email_id = ?
        """, (
            task["subject"],
            task["task"],
            task["deadline"],
            task["sender"],
            task["id"]
        ))

    conn.commit()
    conn.close()


def load_tasks_from_database():

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT *
        FROM tasks
        ORDER BY id DESC
    """)

    rows = cursor.fetchall()

    conn.close()

    return [dict(row) for row in rows]


def update_task_completion(email_id, completed):

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE tasks
        SET completed = ?
        WHERE email_id = ?
    """, (
        1 if completed else 0,
        email_id
    ))

    conn.commit()
    conn.close()


# ============================================================
# GEMINI
# ============================================================

gemini_client = None

try:

    gemini_api_key = st.secrets["GEMINI_API_KEY"]

    gemini_client = genai.Client(
        api_key=gemini_api_key
    )

except Exception:
    gemini_client = None


def generate_gemini_response(prompt):

    if gemini_client is None:

        return None, "Gemini client could not be created."

    try:

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt
        )

        return response.text, None

    except Exception as e:

        error_text = str(e)

        if (
            "429" in error_text
            or "RESOURCE_EXHAUSTED" in error_text
        ):

            return None, (
                "⚠️ Gemini daily quota has been exceeded.\n\n"
                "Please wait until the Gemini quota resets."
            )

        return None, error_text


# ============================================================
# GMAIL AUTHENTICATION
# ============================================================

def get_gmail_service():

    try:
        token_data = json.loads(st.secrets["GMAIL_TOKEN_JSON"])

        creds = Credentials.from_authorized_user_info(
            token_data,
            SCOPES
        )

        if creds.expired and creds.refresh_token:
            creds.refresh(Request())

        return build(
            "gmail",
            "v1",
            credentials=creds
        )

    except Exception as e:
        st.error(f"Gmail connection failed: {e}")
        return None
# ============================================================
# GMAIL MESSAGE LIST
# ============================================================

def get_gmail_messages(
    service,
    max_results=100
):

    results = service.users().messages().list(
        userId="me",
        maxResults=max_results
    ).execute()

    return results.get(
        "messages",
        []
    )


# ============================================================
# EMAIL BODY EXTRACTION
# ============================================================

def get_message_body(payload):

    if "parts" in payload:

        for part in payload["parts"]:

            mime_type = part.get(
                "mimeType",
                ""
            )

            if mime_type == "text/plain":

                data = part.get(
                    "body",
                    {}
                ).get("data")

                if data:

                    try:

                        return base64.urlsafe_b64decode(
                            data
                        ).decode(
                            "utf-8",
                            errors="ignore"
                        )

                    except Exception:
                        pass

            if "parts" in part:

                nested_body = get_message_body(
                    part
                )

                if nested_body:
                    return nested_body

    data = payload.get(
        "body",
        {}
    ).get("data")

    if data:

        try:

            return base64.urlsafe_b64decode(
                data
            ).decode(
                "utf-8",
                errors="ignore"
            )

        except Exception:
            return ""

    return ""


# ============================================================
# GET FULL EMAIL DETAILS
# ============================================================

def get_email_details(
    service,
    message_id
):

    message = service.users().messages().get(
        userId="me",
        id=message_id,
        format="full"
    ).execute()

    payload = message.get(
        "payload",
        {}
    )

    headers = payload.get(
        "headers",
        []
    )

    subject = ""
    sender = ""
    date = ""
    message_id_header = ""
    references_header = ""

    for header in headers:

        name = header.get(
            "name",
            ""
        ).lower()

        value = header.get(
            "value",
            ""
        )

        if name == "subject":
            subject = value

        elif name == "from":
            sender = value

        elif name == "date":
            date = value

        elif name == "message-id":
            message_id_header = value

        elif name == "references":
            references_header = value

    body = get_message_body(
        payload
    )

    return {
        "id": message_id,
        "thread_id": message.get(
            "threadId",
            ""
        ),
        "message_id_header": message_id_header,
        "references_header": references_header,
        "subject": subject,
        "sender": sender,
        "date": date,
        "body": body
    }


# ============================================================
# ACTUAL GMAIL REPLY SENDING
# ============================================================

def send_gmail_reply(
    service,
    original_email,
    reply_text
):

    try:

        message = MIMEText(
            reply_text,
            "plain",
            "utf-8"
        )

        sender = original_email[
            "sender"
        ]

        subject = original_email[
            "subject"
        ]

        # Recipient
        message["To"] = sender

        # Reply subject
        if subject.lower().startswith("re:"):

            message["Subject"] = subject

        else:

            message["Subject"] = (
                "Re: " + subject
            )

        # Message threading
        message_id_header = original_email.get(
            "message_id_header",
            ""
        )

        references_header = original_email.get(
            "references_header",
            ""
        )

        if message_id_header:

            message["In-Reply-To"] = (
                message_id_header
            )

            if references_header:

                message["References"] = (
                    references_header
                    + " "
                    + message_id_header
                )

            else:

                message["References"] = (
                    message_id_header
                )

        # Encode MIME message
        raw_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode("utf-8")

        send_body = {
            "raw": raw_message,
            "threadId": original_email.get(
                "thread_id"
            )
        }

        # Actual Gmail send API
        sent_message = service.users().messages().send(
            userId="me",
            body=send_body
        ).execute()

        return sent_message, None

    except HttpError as e:

        return None, (
            f"Gmail error: {e}"
        )

    except Exception as e:

        return None, (
            f"Error sending email: {e}"
        )


# ============================================================
# EMAIL CLASSIFICATION
# ============================================================

def classify_email(email):

    text = (
        email["subject"]
        + " "
        + email["sender"]
        + " "
        + email["body"]
    ).lower()

    urgent_words = [
        "urgent",
        "immediately",
        "asap",
        "important",
        "emergency"
    ]

    job_words = [
        "job",
        "career",
        "interview",
        "recruitment",
        "application",
        "internship",
        "offer letter"
    ]

    college_words = [
        "college",
        "university",
        "exam",
        "assignment",
        "project",
        "class",
        "semester",
        "faculty"
    ]

    finance_words = [
        "bank",
        "payment",
        "invoice",
        "money",
        "transaction",
        "salary",
        "account"
    ]

    security_words = [
        "security",
        "password",
        "login",
        "verification",
        "verify",
        "sign-in",
        "suspicious"
    ]

    shopping_words = [
        "order",
        "shopping",
        "delivery",
        "amazon",
        "flipkart",
        "product"
    ]

    personal_words = [
        "friend",
        "family",
        "birthday",
        "personal"
    ]

    urgency = "Normal"

    for word in urgent_words:

        if word in text:

            urgency = "Urgent"
            break

    category = "General"

    if any(word in text for word in job_words):

        category = "Job"

    elif any(word in text for word in college_words):

        category = "College"

    elif any(word in text for word in finance_words):

        category = "Finance"

    elif any(word in text for word in security_words):

        category = "Security"

    elif any(word in text for word in shopping_words):

        category = "Shopping"

    elif any(word in text for word in personal_words):

        category = "Personal"

    if (
        urgency == "Urgent"
        or category in [
            "Job",
            "College",
            "Finance",
            "Security"
        ]
    ):

        importance = "Important"

    else:

        importance = "Normal"

    return {
        "importance": importance,
        "urgency": urgency,
        "category": category
    }


# ============================================================
# TASK DETECTION
# ============================================================

def detect_task(email):

    text = (
        email["subject"]
        + " "
        + email["body"]
    ).lower()

    task_patterns = [
        (
            "submit",
            "Submit the required work"
        ),
        (
            "complete",
            "Complete the required task"
        ),
        (
            "register",
            "Complete registration"
        ),
        (
            "interview",
            "Attend the interview"
        ),
        (
            "application",
            "Complete the application"
        ),
        (
            "confirm",
            "Confirm the request"
        ),
        (
            "attend",
            "Attend the event/meeting"
        ),
        (
            "meeting",
            "Attend the meeting"
        ),
        (
            "reply",
            "Reply to the email"
        ),
        (
            "respond",
            "Respond to the email"
        ),
        (
            "action required",
            "Complete the required action"
        )
    ]

    for keyword, task in task_patterns:

        if keyword in text:

            return task

    return None


# ============================================================
# DEADLINE DETECTION
# ============================================================

def detect_deadline(email):

    text = (
        email["subject"]
        + " "
        + email["body"]
    ).lower()

    if "today" in text:

        return "Today"

    if "tomorrow" in text:

        return "Tomorrow"

    weekdays = [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday"
    ]

    for day in weekdays:

        if day in text:

            return day.capitalize()

    date_patterns = [

        r"\b\d{1,2}\s+(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{4}\b",

        r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\.?\s+\d{4}\b",

        r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}\b",

        r"\b\d{1,2}/\d{1,2}/\d{4}\b",

        r"\b\d{1,2}-\d{1,2}-\d{4}\b",

        r"\b\d{4}-\d{1,2}-\d{1,2}\b"
    ]

    for pattern in date_patterns:

        match = re.search(
            pattern,
            text
        )

        if match:

            return match.group(0)

    return "No deadline detected"


# ============================================================
# LANGGRAPH AGENTS
# ============================================================

def email_analysis_agent(state):

    email = state["email"]

    prompt = f"""
Analyze this email.

Subject:
{email["subject"]}

Sender:
{email["sender"]}

Email:
{email["body"]}

Return exactly:

Importance:
Urgency:
Category:
Task:
Deadline:
Reply Needed:
"""

    result, error = generate_gemini_response(
        prompt
    )

    if error:

        return {
            "ai_analysis": error
        }

    return {
        "ai_analysis": result
    }


def task_agent(state):

    email = state["email"]

    task = detect_task(email)

    deadline = detect_deadline(email)

    if task:

        task_data = {
            "id": email["id"],
            "subject": email["subject"],
            "task": task,
            "deadline": deadline,
            "sender": email["sender"]
        }

        save_task_to_database(
            task_data
        )

        return {
            "task_result": task_data
        }

    return {
        "task_result": None
    }


def reply_agent(state):

    return {
        "reply_result": (
            "Reply generation is handled "
            "through the AI Reply button."
        )
    }


def build_email_agent():

    workflow = StateGraph(dict)

    workflow.add_node(
        "email_analysis",
        email_analysis_agent
    )

    workflow.add_node(
        "task_agent",
        task_agent
    )

    workflow.add_node(
        "reply_agent",
        reply_agent
    )

    workflow.add_edge(
        START,
        "email_analysis"
    )

    workflow.add_edge(
        "email_analysis",
        "task_agent"
    )

    workflow.add_edge(
        "task_agent",
        "reply_agent"
    )

    workflow.add_edge(
        "reply_agent",
        END
    )

    return workflow.compile()


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.title("⚙️ Controls")

st.sidebar.header(
    "🔎 Search & Filters"
)

search_text = st.sidebar.text_input(
    "Search emails",
    placeholder=(
        "Search subject, sender or content..."
    ),
    key="email_search"
)

filter_option = st.sidebar.selectbox(
    "Filter emails",
    [
        "All",
        "Important",
        "Urgent",
        "Job",
        "College",
        "Security",
        "Finance",
        "Shopping",
        "General"
    ],
    key="email_filter"
)


# ============================================================
# HEADER
# ============================================================

st.title(
    "📧 AI Email Management Agent"
)

st.caption(
    "Intelligent email analysis, task management "
    "and AI-assisted replies"
)


# ============================================================
# GMAIL CONNECTION
# ============================================================

try:

    service = get_gmail_service()

    st.session_state.gmail_connected = True

    st.success(
        "✅ Gmail connected successfully"
    )

except Exception as e:

    service = None

    st.session_state.gmail_connected = False

    st.error(
        f"Gmail connection failed: {e}"
    )


# ============================================================
# LOAD EMAILS
# ============================================================

emails = []

if service:

    try:

        messages = get_gmail_messages(
            service,
            max_results=100
        )

        for message in messages:

            try:

                email = get_email_details(
                    service,
                    message["id"]
                )

                emails.append(email)

            except Exception:
                continue

        st.session_state.email_cache = emails

    except Exception as e:

        st.error(
            f"Unable to load Gmail messages: {e}"
        )

else:

    emails = st.session_state.email_cache


# ============================================================
# CLASSIFY EMAILS
# ============================================================

for email in emails:

    classification = classify_email(
        email
    )

    email.update(
        classification
    )

    task = detect_task(email)

    deadline = detect_deadline(email)

    if task:

        task_data = {
            "id": email["id"],
            "subject": email["subject"],
            "task": task,
            "deadline": deadline,
            "sender": email["sender"]
        }

        save_task_to_database(
            task_data
        )


# ============================================================
# FILTER EMAILS
# ============================================================

filtered_emails = []

for email in emails:

    combined_text = (
        email["subject"]
        + " "
        + email["sender"]
        + " "
        + email["body"]
    ).lower()

    if search_text:

        if search_text.lower() not in combined_text:

            continue

    if filter_option == "Important":

        if email["importance"] != "Important":
            continue

    elif filter_option == "Urgent":

        if email["urgency"] != "Urgent":
            continue

    elif filter_option in [
        "Job",
        "College",
        "Security",
        "Finance",
        "Shopping",
        "General"
    ]:

        if email["category"] != filter_option:
            continue

    filtered_emails.append(email)


# ============================================================
# DASHBOARD METRICS
# ============================================================

important_count = sum(
    1
    for email in emails
    if email["importance"] == "Important"
)

urgent_count = sum(
    1
    for email in emails
    if email["urgency"] == "Urgent"
)

col1, col2, col3, col4 = st.columns(4)

with col1:

    st.metric(
        "📨 Total Emails",
        len(emails)
    )

with col2:

    st.metric(
        "⭐ Important",
        important_count
    )

with col3:

    st.metric(
        "🚨 Urgent",
        urgent_count
    )

with col4:

    st.metric(
        "🔎 Filtered",
        len(filtered_emails)
    )


st.divider()


# ============================================================
# SMART INBOX
# ============================================================

st.header(
    "📥 Smart Inbox"
)

if not filtered_emails:

    st.info(
        "No emails found."
    )


# ============================================================
# EMAIL CARDS
# ============================================================

for email_index, email in enumerate(
    filtered_emails
):

    with st.container(
        border=True
    ):

        st.subheader(
            "📩 " + (
                email["subject"]
                if email["subject"]
                else "(No Subject)"
            )
        )

        c1, c2, c3 = st.columns(3)

        with c1:

            st.write(
                f"**Importance:** "
                f"{email['importance']}"
            )

        with c2:

            st.write(
                f"**Urgency:** "
                f"{email['urgency']}"
            )

        with c3:

            st.write(
                f"**Category:** "
                f"{email['category']}"
            )

        st.write(
            f"👤 **From:** {email['sender']}"
        )

        st.write(
            f"📅 **Date:** {email['date']}"
        )


        # ----------------------------------------------------
        # VIEW EMAIL
        # ----------------------------------------------------

        with st.expander(
            "👁️ View Email"
        ):

            st.write(
                f"**Subject:** {email['subject']}"
            )

            st.write(
                f"**Sender:** {email['sender']}"
            )

            st.write(
                f"**Date:** {email['date']}"
            )

            st.write("---")

            st.write(
                email["body"]
            )

            st.write("---")

            st.write(
                f"**Importance:** "
                f"{email['importance']}"
            )

            st.write(
                f"**Urgency:** "
                f"{email['urgency']}"
            )

            st.write(
                f"**Category:** "
                f"{email['category']}"
            )


        # ----------------------------------------------------
        # ANALYZE BUTTON
        # ----------------------------------------------------

        analyze_key = (
            "analyze_"
            + str(email["id"])
            + "_"
            + str(email_index)
        )

        if st.button(
            "🔍 Analyze Email",
            key=analyze_key
        ):

            classification = classify_email(
                email
            )

            task = detect_task(
                email
            )

            deadline = detect_deadline(
                email
            )

            result = {
                "importance": classification[
                    "importance"
                ],
                "urgency": classification[
                    "urgency"
                ],
                "category": classification[
                    "category"
                ],
                "task": task
                if task
                else "No task detected",
                "deadline": deadline
            }

            analysis_key = (
                str(email["id"])
                + "_"
                + str(email_index)
            )

            st.session_state.analysis_results[
                analysis_key
            ] = result

            add_activity(
                "🔍 Email analyzed",
                email["subject"]
            )


        # ----------------------------------------------------
        # SHOW ANALYSIS
        # ----------------------------------------------------

        analysis_key = (
            str(email["id"])
            + "_"
            + str(email_index)
        )

        if analysis_key in st.session_state.analysis_results:

            result = (
                st.session_state.analysis_results[
                    analysis_key
                ]
            )

            st.info(
                f"""
**AI Email Analysis**

⭐ Importance: {result["importance"]}

🚨 Urgency: {result["urgency"]}

📂 Category: {result["category"]}

✅ Task: {result["task"]}

📅 Deadline: {result["deadline"]}
"""
            )


        # ----------------------------------------------------
        # AI SUMMARY
        # ----------------------------------------------------

        summary_button_key = (
            "summary_button_"
            + str(email["id"])
            + "_"
            + str(email_index)
        )

        if st.button(
            "📝 Generate AI Summary",
            key=summary_button_key
        ):

            summary_prompt = f"""
Summarize the following email in simple
and clear language.

Subject:
{email["subject"]}

Sender:
{email["sender"]}

Email:
{email["body"]}

Give the summary in 2-3 sentences.

Mention:
1. Main purpose
2. Important information
3. Required action
4. Deadline if present
"""

            with st.spinner(
                "Generating email summary..."
            ):

                summary_text, summary_error = (
                    generate_gemini_response(
                        summary_prompt
                    )
                )

            if summary_error:

                st.error(
                    summary_error
                )

            else:

                summary_key = (
                    str(email["id"])
                    + "_"
                    + str(email_index)
                )

                st.session_state.summary_results[
                    summary_key
                ] = summary_text

                add_activity(
                    "📝 Email summary generated",
                    email["subject"]
                )


        summary_key = (
            str(email["id"])
            + "_"
            + str(email_index)
        )

        if summary_key in st.session_state.summary_results:

            st.success(
                "📝 AI Summary"
            )

            st.write(
                st.session_state.summary_results[
                    summary_key
                ]
            )


        # ----------------------------------------------------
        # RUN AI AGENT
        # ----------------------------------------------------

        run_agent_key = (
            "run_agent_"
            + str(email["id"])
            + "_"
            + str(email_index)
        )

        if st.button(
            "🤖 Run AI Agent",
            key=run_agent_key
        ):

            with st.spinner(
                "AI agents are working..."
            ):

                try:

                    agent = build_email_agent()

                    result = agent.invoke(
                        {
                            "email": email
                        }
                    )

                    agent_key = (
                        str(email["id"])
                        + "_"
                        + str(email_index)
                    )

                    st.session_state.agent_results[
                        agent_key
                    ] = result

                    add_activity(
                        "🤖 AI Agent executed",
                        email["subject"]
                    )

                except Exception as e:

                    st.error(
                        f"Agent error: {e}"
                    )


        agent_key = (
            str(email["id"])
            + "_"
            + str(email_index)
        )

        if agent_key in st.session_state.agent_results:

            agent_result = (
                st.session_state.agent_results[
                    agent_key
                ]
            )

            if agent_result.get(
                "ai_analysis"
            ):

                st.info(
                    "🤖 AI Agent Analysis"
                )

                st.write(
                    agent_result[
                        "ai_analysis"
                    ]
                )

            if agent_result.get(
                "task_result"
            ):

                st.success(
                    "✅ Task detected and saved."
                )


        # ----------------------------------------------------
        # AI REPLY GENERATION
        # ----------------------------------------------------

        generate_reply_key = (
            "generate_reply_"
            + str(email["id"])
            + "_"
            + str(email_index)
        )

        reply_key = (
            "reply_"
            + str(email["id"])
            + "_"
            + str(email_index)
        )

        if st.button(
            "✉️ Generate AI Reply",
            key=generate_reply_key
        ):

            reply_prompt = f"""
Write a professional and polite reply
to this email.

Subject:
{email["subject"]}

Sender:
{email["sender"]}

Email:
{email["body"]}

Requirements:
- Be clear and concise.
- Be professional.
- Answer the sender appropriately.
- Do not add unnecessary information.
- Return only the reply text.
"""

            with st.spinner(
                "Generating AI reply..."
            ):

                reply_text, reply_error = (
                    generate_gemini_response(
                        reply_prompt
                    )
                )

            if reply_error:

                st.error(
                    reply_error
                )

            else:

                st.session_state[
                    reply_key
                ] = reply_text

                st.session_state.reply_results[
                    str(email["id"])
                    + "_"
                    + str(email_index)
                ] = reply_text

                add_activity(
                    "✉️ AI reply generated",
                    email["subject"]
                )


        # ----------------------------------------------------
        # REPLY EDITOR
        # ----------------------------------------------------

        if reply_key in st.session_state:

            st.subheader(
                "✏️ Reply Editor"
            )

            st.caption(
                "Edit the AI-generated reply before sending."
            )

            edited_reply = st.text_area(
                "Reply",
                key=reply_key,
                height=180
            )

            # ------------------------------------------------
            # SEND SECTION
            # ------------------------------------------------

            st.markdown(
                "### 📤 Send Reply"
            )

            confirm_key = (
                "confirm_send_"
                + str(email["id"])
                + "_"
                + str(email_index)
            )

            send_button_key = (
                "send_reply_"
                + str(email["id"])
                + "_"
                + str(email_index)
            )

            sent_key = (
                "reply_sent_"
                + str(email["id"])
                + "_"
                + str(email_index)
            )

            confirm_send = st.checkbox(
                "I confirm that I want to send this reply",
                key=confirm_key
            )

            if confirm_send:

                if st.button(
                    "📤 Send Reply",
                    key=send_button_key
                ):

                    if not edited_reply.strip():

                        st.warning(
                            "Please enter a reply before sending."
                        )

                    elif not service:

                        st.error(
                            "Gmail is not connected."
                        )

                    else:

                        with st.spinner(
                            "Sending reply through Gmail..."
                        ):

                            sent_message, send_error = (
                                send_gmail_reply(
                                    service,
                                    email,
                                    edited_reply
                                )
                            )

                        if send_error:

                            st.error(
                                send_error
                            )

                        else:

                            st.session_state[
                                sent_key
                            ] = True

                            add_activity(
                                "📤 Email reply sent",
                                email["subject"]
                            )

                            st.success(
                                "✅ Reply sent successfully through Gmail!"
                            )

                            st.info(
                                "You can check Gmail → Sent to verify it."
                            )


            if st.session_state.get(
                sent_key,
                False
            ):

                st.success(
                    "📨 This reply has been sent."
                )


        st.divider()


# ============================================================
# TASK MANAGER
# ============================================================

st.header(
    "✅ Task Manager"
)

tasks = load_tasks_from_database()

total_tasks = len(tasks)

completed_tasks = sum(
    1
    for task in tasks
    if task["completed"] == 1
)

pending_tasks = (
    total_tasks
    - completed_tasks
)

t1, t2, t3 = st.columns(3)

with t1:

    st.metric(
        "Total Tasks",
        total_tasks
    )

with t2:

    st.metric(
        "Pending",
        pending_tasks
    )

with t3:

    st.metric(
        "Completed",
        completed_tasks
    )


if tasks:

    for task_index, task in enumerate(tasks):

        task_checkbox_key = (
            "task_checkbox_"
            + str(task["id"])
            + "_"
            + str(task_index)
        )

        completed = st.checkbox(
            (
                "✅ "
                if task["completed"]
                else "⬜ "
            )
            + task["task"]
            + " — "
            + task["subject"],
            value=bool(
                task["completed"]
            ),
            key=task_checkbox_key
        )

        if completed != bool(
            task["completed"]
        ):

            update_task_completion(
                task["email_id"],
                completed
            )

            st.rerun()

        st.caption(
            f"📅 Deadline: {task['deadline']} | "
            f"👤 {task['sender']}"
        )

else:

    st.info(
        "No tasks detected yet."
    )


st.divider()


# ============================================================
# CALENDAR / DEADLINES
# ============================================================

st.header(
    "📅 Calendar & Deadlines"
)

deadlines = []

for task in tasks:

    if task["deadline"] != "No deadline detected":

        deadlines.append(task)


today_deadlines = [
    task
    for task in deadlines
    if task["deadline"].lower()
    == "today"
]

tomorrow_deadlines = [
    task
    for task in deadlines
    if task["deadline"].lower()
    == "tomorrow"
]

other_deadlines = [
    task
    for task in deadlines
    if task not in today_deadlines
    and task not in tomorrow_deadlines
]


c1, c2, c3 = st.columns(3)

with c1:

    st.metric(
        "Today",
        len(today_deadlines)
    )

with c2:

    st.metric(
        "Tomorrow",
        len(tomorrow_deadlines)
    )

with c3:

    st.metric(
        "Other Deadlines",
        len(other_deadlines)
    )


if deadlines:

    for task in deadlines:

        st.write(
            f"📅 **{task['deadline']}** — "
            f"{task['task']} "
            f"({task['subject']})"
        )

else:

    st.info(
        "No deadlines detected."
    )


st.divider()


# ============================================================
# ACTIVITY HISTORY
# ============================================================

st.header(
    "🕘 Activity History"
)

history = load_activity_history()

if history:

    for item in history[:30]:

        st.write(
            f"**{item['action']}** — "
            f"{item['details']}  \n"
            f"🕐 {item['created_at']}"
        )

        st.divider()

else:

    st.info(
        "No activity yet."
    )


if st.button(
    "🗑️ Clear Activity History"
):

    clear_activity_history()

    st.success(
        "Activity history cleared."
    )

    st.rerun()


# ============================================================
# FOOTER
# ============================================================

st.divider()

st.caption(
    "AI Email Management Agent | "
    "Python + Streamlit + Gemini + LangGraph + Gmail API + SQLite"
)
