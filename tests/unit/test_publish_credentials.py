"""Per-requester publish credential interpretation (#635).

Credential was a single server environment variable. Then any authenticated user
could publish as server owner's Hugging Face / Kaggle account — provider credential
already has per-principal storage (ADR 0012) but publish path bypassed it to read
environment directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kpubdata_builder.credentials.crypto import AesGcmCredentialCipher
from kpubdata_builder.credentials.store import SQLiteCredentialRepository
from kpubdata_builder.service.publish_credentials import (
    PUBLISH_CREDENTIAL_SLOTS,
    _slot,
    resolve_publish_credentials,
)


class _Repo:
    """Fake repository that does not validate slot names.

    **This fake hid the bug.** Original implementation created slot as ``publish:huggingface:HF_TOKEN``, but real store validates provider key with ``^[a-z0-9][a-z0-9_-]{0,63}$`` raising ValueError. Fake just dict lookup, so passed. Below ``TestAgainstTheRealStore`` re-verifies same contract against actual SQLite store.
    """

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        return self._secrets.get(f"{owner_id}|{provider}")


class _BrokenRepo:
    def get_secret(self, owner_id: str, provider: str) -> str | None:
        raise RuntimeError("credential backend unavailable")


class TestResolution:
    def test_a_stored_credential_wins_over_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")
        repo = _Repo({f"oidc:a|{_slot('huggingface', 'HF_TOKEN')}": "her-own-token"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface").values == {
            "HF_TOKEN": "her-own-token"
        }

    def test_the_server_token_is_still_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # In single-user deployment, one global token is correct configuration — do not break it.
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), "oidc:a", "huggingface").values == {
            "HF_TOKEN": "server-token"
        }

    def test_no_credential_anywhere_resolves_to_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HF_TOKEN", raising=False)

        assert resolve_publish_credentials(_Repo({}), "oidc:a", "huggingface").values == {}

    def test_an_anonymous_caller_falls_back_to_the_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), None, "huggingface").values == {
            "HF_TOKEN": "server-token"
        }

    def test_a_paired_credential_is_never_half_stored_half_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mixing one requester's and one server's makes it unclear which account."""
        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")
        repo = _Repo({f"oidc:a|{_slot('kaggle', 'KAGGLE_USERNAME')}": "her-user"})

        assert resolve_publish_credentials(repo, "oidc:a", "kaggle").values == {
            "KAGGLE_USERNAME": "her-user"
        }

    def test_local_publishing_needs_no_credential(self) -> None:
        assert resolve_publish_credentials(_Repo({}), "oidc:a", "local").values == {}

    def test_a_publish_slot_does_not_collide_with_a_provider_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same owner must be able to have both datago credential and HF token.
        # publish does not pick that up even if provider slot has a value.
        monkeypatch.delenv("HF_TOKEN", raising=False)
        repo = _Repo({"oidc:a|huggingface": "provider-shaped-value"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface").values == {}


class TestPublisherPrefersPassedCredentials:
    def test_huggingface_uses_the_passed_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        import types

        captured: dict[str, object] = {}

        class _Api:
            def __init__(self, token: str | None = None) -> None:
                captured["token"] = token

            def create_repo(self, **_kwargs: object) -> None: ...
            def upload_file(self, **_kwargs: object) -> None: ...
            def upload_folder(self, **_kwargs: object) -> None: ...

        module = types.ModuleType("huggingface_hub")
        module.HfApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        HuggingFacePublisher().publish(
            (),
            destination="kpubdata/x",
            credentials={"HF_TOKEN": "her-own-token"},
        )

        assert captured["token"] == "her-own-token"

    def test_huggingface_falls_back_to_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types

        captured: dict[str, object] = {}

        class _Api:
            def __init__(self, token: str | None = None) -> None:
                captured["token"] = token

            def create_repo(self, **_kwargs: object) -> None: ...
            def upload_file(self, **_kwargs: object) -> None: ...
            def upload_folder(self, **_kwargs: object) -> None: ...

        module = types.ModuleType("huggingface_hub")
        module.HfApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        HuggingFacePublisher().publish((), destination="kpubdata/x")

        assert captured["token"] == "server-token"

    def test_huggingface_refuses_when_no_token_exists_anywhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types

        module = types.ModuleType("huggingface_hub")
        module.HfApi = object  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        monkeypatch.delenv("HF_TOKEN", raising=False)

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        with pytest.raises(RuntimeError, match="No Hugging Face API token"):
            HuggingFacePublisher().publish((), destination="kpubdata/x")


class TestAgainstTheRealStore:
    """Verify with actual SQLite repository, not fake (#635, #655)."""

    @staticmethod
    def _repository(tmp_path: Path) -> SQLiteCredentialRepository:
        return SQLiteCredentialRepository(
            tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
        )

    @pytest.mark.parametrize("target", ["huggingface", "kaggle"])
    def test_every_slot_name_is_storable(self, tmp_path: Path, target: str) -> None:
        repository = self._repository(tmp_path)

        for variable in PUBLISH_CREDENTIAL_SLOTS[target]:
            slot = _slot(target, variable)
            repository.put("oidc:a", slot, f"value-for-{variable}")

            assert repository.get_secret("oidc:a", slot) == f"value-for-{variable}"

    def test_a_stored_token_round_trips_through_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")
        repository = self._repository(tmp_path)
        repository.put("oidc:a", _slot("huggingface", "HF_TOKEN"), "her-own-token")

        assert resolve_publish_credentials(repository, "oidc:a", "huggingface").values == {
            "HF_TOKEN": "her-own-token"
        }

    def test_an_owner_without_a_stored_token_still_gets_the_server_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(
            self._repository(tmp_path), "oidc:b", "huggingface"
        ).values == {"HF_TOKEN": "server-token"}

    def test_a_publish_slot_never_collides_with_a_provider_slot(self, tmp_path: Path) -> None:
        # Same owner must be able to have both datago provider key and HF token.
        repository = self._repository(tmp_path)
        repository.put("oidc:a", "datago", "provider-key")
        repository.put("oidc:a", _slot("huggingface", "HF_TOKEN"), "hf-token")

        assert repository.get_secret("oidc:a", "datago") == "provider-key"
        assert repository.get_secret("oidc:a", _slot("huggingface", "HF_TOKEN")) == "hf-token"


class TestABrokenStoreDoesNotBreakPublishing:
    def test_a_failing_lookup_falls_back_to_the_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If repository failure becomes publish failure, a deployment using only global token breaks
        # the moment credential backend is enabled.
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_BrokenRepo(), "oidc:a", "huggingface").values == {
            "HF_TOKEN": "server-token"
        }


class TestKaggleUsesTheCredentialsItIsGiven:
    def test_the_passed_credentials_reach_the_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If argument is accepted but not used, all publishing goes under server account."""
        import sys
        import types

        seen: dict[str, str | None] = {}

        class _Api:
            def authenticate(self) -> None:
                seen["user"] = os.environ.get("KAGGLE_USERNAME")
                seen["key"] = os.environ.get("KAGGLE_KEY")
                raise RuntimeError("stop here — authentication is all we need to observe")

        module = types.ModuleType("kaggle.api.kaggle_api_extended")
        module.KaggleApi = _Api  # type: ignore[attr-defined]
        parent = types.ModuleType("kaggle")
        api_pkg = types.ModuleType("kaggle.api")
        monkeypatch.setitem(sys.modules, "kaggle", parent)
        monkeypatch.setitem(sys.modules, "kaggle.api", api_pkg)
        monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", module)
        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")

        from kpubdata_builder.publishers.kaggle import KagglePublisher

        with pytest.raises(Exception):  # noqa: B017 - authenticate breakpoint.
            KagglePublisher().publish(
                (),
                destination="owner/name",
                credentials={"KAGGLE_USERNAME": "her-user", "KAGGLE_KEY": "her-key"},
            )

        assert seen == {"user": "her-user", "key": "her-key"}

    def test_the_environment_is_restored_afterwards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # One request's credentials must not remain for the next request.
        import sys
        import types

        class _Api:
            def authenticate(self) -> None:
                raise RuntimeError("stop")

        module = types.ModuleType("kaggle.api.kaggle_api_extended")
        module.KaggleApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kaggle", types.ModuleType("kaggle"))
        monkeypatch.setitem(sys.modules, "kaggle.api", types.ModuleType("kaggle.api"))
        monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", module)
        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")

        from kpubdata_builder.publishers.kaggle import KagglePublisher

        with pytest.raises(Exception):  # noqa: B017
            KagglePublisher().publish(
                (), destination="owner/name", credentials={"KAGGLE_USERNAME": "her-user"}
            )

        assert os.environ["KAGGLE_USERNAME"] == "server-user"


class TestClosingTheServerFallback:
    """Multi-user deployment must be able to close server token fallback (#635).

    While fallback is open, principal without stored credential still publishes as server owner — adding per-requester credential alone does not close original issue.
    """

    def test_the_fallback_is_open_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # In single-user deployment, one global token is correct. Flip the default and
        # that deployment silently stops publishing.
        monkeypatch.delenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", raising=False)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), "oidc:b", "huggingface").values == {
            "HF_TOKEN": "server-token"
        }

    def test_closing_it_leaves_a_principal_without_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "true")
        monkeypatch.setenv("HF_TOKEN", "server-token")

        resolution = resolve_publish_credentials(_Repo({}), "oidc:b", "huggingface")

        assert resolution.values == {}
        # Empty alone is not enough — if caller thinks "then just don't pass it",
        # publisher falls back to env var. Must say reject.
        assert resolution.refused is True

    def test_closing_it_does_not_affect_a_principal_with_their_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "true")
        monkeypatch.setenv("HF_TOKEN", "server-token")
        repo = _Repo({f"oidc:a|{_slot('huggingface', 'HF_TOKEN')}": "hers"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface").values == {
            "HF_TOKEN": "hers"
        }


class TestTheSwitchActuallyBlocksPublishing:
    """Verify ``REQUIRE_OWN_PUBLISH_CREDENTIAL=true`` actually blocks publishing.

    resolver returned {} from start and unit test passed. But publish_api omitted kwarg if credentials empty by if credentials:, and publisher read os.environ without args. So even with switch on, any authenticated user could publish as server account — all three pieces passed individual tests.
    """

    def _hf_api(self, monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
        import sys
        import types

        class _Api:
            def __init__(self, token: str | None = None) -> None:
                captured["token"] = token

            def create_repo(self, **_kwargs: object) -> None: ...
            def upload_file(self, **_kwargs: object) -> None: ...
            def upload_folder(self, **_kwargs: object) -> None: ...

        module = types.ModuleType("huggingface_hub")
        module.HfApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    def test_an_empty_mapping_does_not_fall_back_to_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This was the last gate to bypass."""
        captured: dict[str, object] = {}
        self._hf_api(monkeypatch, captured)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        with pytest.raises(RuntimeError, match="No Hugging Face API token"):
            HuggingFacePublisher().publish((), destination="kpubdata/x", credentials={})

        assert "token" not in captured, "서버 토큰으로 API 를 만들면 안 된다"

    def test_omitting_the_argument_still_uses_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI path remains same — when executor and server environment are identical."""
        captured: dict[str, object] = {}
        self._hf_api(monkeypatch, captured)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        HuggingFacePublisher().publish((), destination="kpubdata/x")

        assert captured["token"] == "server-token"

    def test_kaggle_with_an_empty_mapping_hides_the_server_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kaggle SDK only reads environment variables. If not cleared, picks up server account."""
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")

        with _kaggle_environment({}):
            assert "KAGGLE_USERNAME" not in os.environ
            assert "KAGGLE_KEY" not in os.environ

        # Exiting the block restores to original.
        assert os.environ["KAGGLE_USERNAME"] == "server-user"
        assert os.environ["KAGGLE_KEY"] == "server-key"

    def test_kaggle_with_no_argument_leaves_the_environment_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")

        with _kaggle_environment(None):
            assert os.environ["KAGGLE_USERNAME"] == "server-user"


class TestReadinessAgreesWithPublish:
    """readiness and POST must use the same criteria."""

    def test_a_refused_resolution_blocks_readiness(self) -> None:
        from kpubdata_builder.service.publish import credential_blocker
        from kpubdata_builder.service.publish_credentials import PublishCredentialResolution

        issue = credential_blocker("huggingface", PublishCredentialResolution(refused=True))

        assert issue is not None
        assert issue.code == "credential_required"

    def test_a_resolved_credential_clears_readiness(self) -> None:
        from kpubdata_builder.service.publish import credential_blocker
        from kpubdata_builder.service.publish_credentials import PublishCredentialResolution

        resolution = PublishCredentialResolution(values={"HF_TOKEN": "hers"})

        assert credential_blocker("huggingface", resolution) is None

    def test_an_empty_resolution_blocks_readiness(self) -> None:
        from kpubdata_builder.service.publish import credential_blocker
        from kpubdata_builder.service.publish_credentials import PublishCredentialResolution

        issue = credential_blocker("huggingface", PublishCredentialResolution())

        assert issue is not None
        assert issue.code == "credential_unavailable"

    def test_callers_without_a_resolution_keep_the_old_server_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI/existing callers' behavior is unchanged."""
        from kpubdata_builder.service.publish import credential_blocker

        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert credential_blocker("huggingface") is None


class TestKaggleCredentialsDoNotCross:
    """Environment variables belong to process, not thread."""

    def test_concurrent_publishes_each_see_their_own_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os
        import threading
        import time

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")

        observed: list[tuple[str, str]] = []
        start = threading.Barrier(2)

        def _publish_as(user: str) -> None:
            start.wait(timeout=5)
            with _kaggle_environment({"KAGGLE_USERNAME": user, "KAGGLE_KEY": f"{user}-key"}):
                time.sleep(0.05)
                observed.append((user, os.environ["KAGGLE_USERNAME"]))

        threads = [threading.Thread(target=_publish_as, args=(u,)) for u in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(observed) == 2
        assert all(wanted == seen for wanted, seen in observed), observed

    def test_the_server_environment_is_restored_afterwards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")

        with _kaggle_environment({"KAGGLE_USERNAME": "hers", "KAGGLE_KEY": "her-key"}):
            assert os.environ["KAGGLE_USERNAME"] == "hers"

        assert os.environ["KAGGLE_USERNAME"] == "server-user"

    def test_an_empty_mapping_also_hides_the_kaggle_json_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if env is cleared, SDK reads ``~/.kaggle/kaggle.json``."""
        import os
        from pathlib import Path

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.delenv("KAGGLE_CONFIG_DIR", raising=False)

        with _kaggle_environment({}):
            config_dir = os.environ.get("KAGGLE_CONFIG_DIR")
            assert config_dir is not None, "SDK 가 서버의 kaggle.json 을 찾을 수 있다"
            assert list(Path(config_dir).iterdir()) == []

        assert "KAGGLE_CONFIG_DIR" not in os.environ

    def test_the_cli_path_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``None`` means caller did not decide — environment and config unchanged."""
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.delenv("KAGGLE_CONFIG_DIR", raising=False)

        with _kaggle_environment(None):
            assert os.environ["KAGGLE_USERNAME"] == "server-user"
            assert "KAGGLE_CONFIG_DIR" not in os.environ
