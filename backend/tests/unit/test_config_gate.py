"""Settings 的 fail-loud 面（spec §5.1 的 test_config_gate.py）。

生产门实读 app/core/config.py:100-109：app_env == "production" 且 jwt_secret 为空
⇒ import 期 RuntimeError。development 保持现状（运行期首请求由
security._require_secret 炸），这条差异是裁定不是遗漏，四枚针把它钉住。
"""
IMPORT_AND_REPORT = (
    "import app.core.config as c; print('OK', c.settings.app_env)"
)

# 显然非真值：CI 口径与 spec §7 同源，扫密门禁的占位符形状认得它
PLACEHOLDER_SECRET = "test-only-not-a-real-secret"


def test_production_without_jwt_secret_refuses_to_start(run_python):
    rc, out, err = run_python(IMPORT_AND_REPORT, APP_ENV="production", JWT_SECRET="")
    assert rc != 0
    assert "JWT_SECRET" in err
    assert "OK" not in out


def test_production_with_placeholder_secret_starts(run_python):
    rc, out, err = run_python(IMPORT_AND_REPORT, APP_ENV="production",
                              JWT_SECRET=PLACEHOLDER_SECRET)
    assert rc == 0, err
    assert "OK production" in out


def test_development_without_secret_starts_and_defers_to_runtime(run_python):
    rc, out, err = run_python(IMPORT_AND_REPORT, APP_ENV="development", JWT_SECRET="")
    assert rc == 0, err
    assert "OK development" in out


def test_ci_style_environment_has_no_llm_key(run_python):
    """零钱门：CI 不设 LLM_API_KEY，settings 读到的必须是空串。

    断言的是「空」而不是「非空」——真机 .env 里有真 key，靠 env 覆盖赢过 env_file
    这条优先级才成立；一旦 pydantic-settings 改了优先级，这条针会先红。
    """
    rc, out, err = run_python(
        "from app.core.config import settings; print(repr(settings.llm_api_key))",
        LLM_API_KEY="",
    )
    assert rc == 0, err
    assert out.strip() == "''"
