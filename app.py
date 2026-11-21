"""AIライティングツール（Streamlit + Gemini API）

起動: streamlit run app.py
"""

import os
import re
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

from gemini_client import DEFAULT_MODEL, MODEL_CHOICES, stream_generate
from tools import TOOLS, TOOLS_BY_KEY, Tool, _block

load_dotenv()

st.set_page_config(page_title="AIライティングツール", page_icon="✍️", layout="wide")

HISTORY_KEY = "__history__"
HISTORY_LIMIT = 50
TEXTAREA_MAX_CHARS = 20000  # 長文の貼り付けによる API 利用量の増えすぎを防ぐ

# 生成結果を画面に表示する前の無害化。貼り付けた文章に仕込まれた指示で、
# AI が画像やリンクを出力させられた場合の被害を防ぐ。
# リンクは見た目の文字と実際の行き先が違うことがあるため、「文字（URL）」の形にして行き先を見せる
# （文字の中の角かっこは1段階まで許す。例：[公式 [1] サイト](URL)）
_MD_LINK = re.compile(r"(?<!!)\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\(([^)\s]+)[^)]*\)")


def _safe_markdown(text: str) -> str:
    text = _MD_LINK.sub(r"\1（\2）", text)
    # 正規表現は入れ子の深さに限りがあるため、書き換えから漏れたリンクは ]( をエスケープして
    # リンクとして働かなくする（インラインのリンクは必ず ]( を含む）
    text = text.replace("](", "\\](")
    # 画像は表示した瞬間にブラウザが外部URLへアクセスする。画像記法は必ず ![ で始まるので、
    # 書き方を1つずつ探すのではなく ![ をエスケープして、すべての形式をまとめて無効化する
    text = text.replace("![", "!\\[")
    # 参照形式の定義 [r]: URL（引用や箇条書きの中でも有効）も同じ理由で ]: をエスケープして無効化する。
    # 参照形式の画像・リンクは行き先が解決されず、定義行は「[r]: URL」という文字として見える
    return text.replace("]:", "\\]:")

# ------------------------------------------------------------ セッション初期化
st.session_state.setdefault("results", {})  # tool.key -> {"prompt", "output"}
st.session_state.setdefault("history", [])  # 新しい順

# ツールを切り替えても入力内容が消えないように、フォームの値を保持し直す
for _k in [k for k in st.session_state if "__" in str(k) and not str(k).startswith("__")]:
    st.session_state[_k] = st.session_state[_k]


# ------------------------------------------------------------ サイドバー
def render_sidebar() -> tuple[str, str, str, float | None]:
    with st.sidebar:
        st.title("✍️ AIライティング")

        labels = {t.key: f"{t.icon} {t.name}" for t in TOOLS}
        # 選択肢の表示文字を変えると Streamlit が別ウィジェット扱いして選択がリセットされるため、
        # ラベルは固定にする（履歴件数などの可変な値を入れない）
        labels[HISTORY_KEY] = "🕘 履歴"
        selected = st.radio(
            "ツール", list(labels), format_func=labels.get, label_visibility="collapsed", key="nav"
        )

        st.divider()
        with st.expander("⚙️ 設定", expanded=not os.getenv("GEMINI_API_KEY")):
            # .env のキーは入力欄の初期値に入れない（type="password" でも値はブラウザに送られるため）
            env_key = os.getenv("GEMINI_API_KEY", "")
            if env_key:
                st.caption("🔑 .env の APIキーを使用中")
                api_key = env_key
            else:
                api_key = st.text_input(
                    "Gemini APIキー",
                    type="password",
                    help=".env に GEMINI_API_KEY を設定すると、ここに入力する必要はなくなります。",
                )
            env_model = os.getenv("GEMINI_MODEL", DEFAULT_MODEL)
            choices = MODEL_CHOICES if env_model in MODEL_CHOICES else [env_model, *MODEL_CHOICES]
            model = st.selectbox("モデル", choices, index=choices.index(env_model))
            custom_temp = st.checkbox("創造性（temperature）を手動で指定")
            temperature = (
                st.slider("temperature", 0.0, 2.0, 0.7, 0.1) if custom_temp else None
            )
    return selected, api_key, model, temperature


# ------------------------------------------------------------ 入力フォーム
def render_field(tool: Tool, f) -> object:
    key = f"{tool.key}__{f.key}"
    label = f.label + (" *" if f.required else "")
    if f.kind == "textarea":
        return st.text_area(
            label, key=key, height=f.height, placeholder=f.placeholder, help=f.help or None,
            max_chars=TEXTAREA_MAX_CHARS,
        )
    # 既に値が保持されている場合は初期値を渡さない（Streamlit の警告回避）
    if f.kind == "select":
        if key not in st.session_state:
            st.session_state[key] = f.default if f.default in f.options else f.options[0]
        return st.selectbox(label, f.options, key=key, help=f.help or None)
    if f.kind == "number":
        st.session_state.setdefault(key, int(f.default or 1000))
        return st.number_input(label, min_value=100, max_value=20000, step=100, key=key, help=f.help or None)
    return st.text_input(label, key=key, placeholder=f.placeholder, help=f.help or None)


def render_form(tool: Tool) -> dict | None:
    """フォームを描画し、生成ボタンが押されたら入力値を返す。"""
    values: dict = {}
    with st.form(f"form_{tool.key}"):
        long_fields = [f for f in tool.fields if f.kind == "textarea"]
        short_fields = [f for f in tool.fields if f.kind != "textarea"]

        for f in long_fields:
            values[f.key] = render_field(tool, f)
        if short_fields:
            cols = st.columns(min(3, len(short_fields)))
            for i, f in enumerate(short_fields):
                with cols[i % len(cols)]:
                    values[f.key] = render_field(tool, f)

        submitted = st.form_submit_button("✨ 生成する", type="primary", use_container_width=True)

    if not submitted:
        return None
    missing = [f.label for f in tool.fields if f.required and not str(values[f.key]).strip()]
    if missing:
        st.warning(f"必須項目を入力してください：{'、'.join(missing)}")
        return None
    return values


# ------------------------------------------------------------ 生成処理
def generate(tool: Tool, prompt: str, api_key: str, model: str, temperature: float | None) -> str | None:
    if not api_key:
        st.error("Gemini APIキーが設定されていません。サイドバーの「設定」から入力してください。")
        return None
    temp = tool.temperature if temperature is None else temperature
    try:
        # st.write_stream は届いた文章を無害化せずに Markdown 表示するため使わない。
        # ![ などが断片の境目で分かれて届くことがあるので、断片ごとではなく、ためた全文を毎回無害化して表示し直す
        with st.container(border=True):
            placeholder = st.empty()
            output = ""
            for chunk in stream_generate(api_key, model, prompt, tool.system, temp):
                output += chunk
                placeholder.markdown(_safe_markdown(output))
    except Exception as e:  # APIキー誤り・通信エラー・レート制限など
        # 例外の本文には内部情報が含まれることがあるため、型名だけを表示する
        st.error(
            f"生成中にエラーが発生しました（{type(e).__name__}）。"
            "APIキー・モデル名・通信状況を確認してください。"
        )
        return None

    st.session_state.results[tool.key] = {"prompt": prompt, "output": output}
    st.session_state.history.insert(0, {
        "time": datetime.now().strftime("%m/%d %H:%M"),
        "tool": f"{tool.icon} {tool.name}",
        "prompt": prompt,
        "output": output,
    })
    del st.session_state.history[HISTORY_LIMIT:]
    return output


def render_output(output: str, file_stem: str, key: str) -> None:
    st.caption(f"{len(output):,} 文字")
    preview, raw = st.tabs(["プレビュー", "テキスト（コピー用）"])
    with preview:
        with st.container(border=True):
            st.markdown(_safe_markdown(output))
    with raw:
        st.code(output, language=None, wrap_lines=True)
    st.download_button(
        "⬇️ Markdownで保存",
        output,
        file_name=f"{file_stem}_{datetime.now():%Y%m%d_%H%M%S}.md",
        mime="text/markdown",
        key=f"dl_{key}",
    )


# ------------------------------------------------------------ 画面
def tool_page(tool: Tool, api_key: str, model: str, temperature: float | None) -> None:
    st.header(f"{tool.icon} {tool.name}")
    st.caption(tool.description)

    values = render_form(tool)
    if values is not None:
        if generate(tool, tool.build_prompt(values), api_key, model, temperature) is not None:
            st.rerun()

    result = st.session_state.results.get(tool.key)
    if not result:
        return

    st.subheader("生成結果")
    render_output(result["output"], tool.key, tool.key)

    # 結果に対して追加の修正指示を出す
    with st.form(f"refine_{tool.key}", clear_on_submit=True):
        instruction = st.text_input(
            "🔧 この結果を修正する",
            placeholder="例：もっと短く / 結論を先に / もう少しカジュアルに",
        )
        if st.form_submit_button("修正を依頼") and instruction.strip():
            # 前回の出力も _block で囲み、データとして扱わせる（偽の閉じの見出しの置き換えも効かせるため）
            refine_prompt = (
                f"{result['prompt']}\n\n---\n"
                f"{_block('前回の出力', result['output'])}\n\n---\n"
                f"上記の前回の出力を、次の指示に従って修正してください。修正後の全文のみを出力してください。\n"
                f"指示: {instruction}"
            )
            if generate(tool, refine_prompt, api_key, model, temperature) is not None:
                st.rerun()

    if st.button("🗑️ 結果をクリア", key=f"clear_{tool.key}"):
        st.session_state.results.pop(tool.key, None)
        st.rerun()


def history_page() -> None:
    st.header("🕘 履歴")
    st.caption(f"このセッションで生成した結果（最新 {HISTORY_LIMIT} 件まで）です。ブラウザを再読み込みすると消えます。")
    history = st.session_state.history
    if not history:
        st.info("まだ履歴はありません。")
        return
    if st.button("履歴をすべて消去"):
        history.clear()
        st.rerun()
    for i, item in enumerate(history):
        # 見出し（st.expander のラベル）でも画像・リンクの Markdown が表示されるため、同じ無害化を通す
        first_line = _safe_markdown(item["output"].strip().splitlines()[0][:40]) if item["output"].strip() else ""
        with st.expander(f"{item['time']}　{item['tool']}　｜　{first_line}"):
            render_output(item["output"], "history", f"hist_{i}")
            with st.popover("送信したプロンプトを見る"):
                st.code(item["prompt"], language=None, wrap_lines=True)


def main() -> None:
    selected, api_key, model, temperature = render_sidebar()
    if selected == HISTORY_KEY:
        history_page()
    else:
        tool_page(TOOLS_BY_KEY[selected], api_key, model, temperature)


main()
