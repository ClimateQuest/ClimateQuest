"""CPU time measurement and the energy meter middleware (only active with the production settings)."""

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

import pytest
from django.http import HttpResponse, StreamingHttpResponse
from django.test import RequestFactory

from carbon import middleware as meter_module
from carbon.cpu import CpuMeasurement, DbCpuMeter
from carbon.middleware import FLUSH_SECONDS, EnergyMeterMiddleware

# ---- DbCpuMeter ------------------------------------------------------------------------------------


@pytest.fixture
def postgres() -> Iterator[mock.MagicMock]:
    """A PostgreSQL connection served by the backend process 42."""
    with mock.patch("carbon.cpu.connection") as connection:
        connection.vendor = "postgresql"
        connection.connection.get_backend_pid.return_value = 42
        yield connection


def schedstat(content: str | Exception) -> mock._patch:
    """Replace the content of ``/proc/<pid>/schedstat``."""
    kwargs = {"side_effect": content} if isinstance(content, Exception) else {"return_value": content}
    return mock.patch("carbon.cpu.Path.read_text", **kwargs)


def process_times(user: float, system: float) -> mock._patch:
    process = mock.Mock()
    process.return_value.cpu_times.return_value = mock.Mock(user=user, system=system)
    return mock.patch("carbon.cpu.psutil.Process", process)


def test_new_meter_reads_nothing() -> None:
    meter = DbCpuMeter()
    assert (meter.pid, meter.source, meter.read_ns()) == (None, "none", 0)


def test_sqlite_runs_inside_django_and_is_not_measured_separately() -> None:
    meter = DbCpuMeter()
    meter.pid, meter.source = 7, "psutil"  # left over from an earlier PostgreSQL connection
    with mock.patch("carbon.cpu.connection") as connection:
        connection.vendor = "sqlite"
        meter.attach()
    connection.ensure_connection.assert_not_called()
    assert (meter.pid, meter.source, meter.read_ns()) == (None, "none", 0)


def test_schedstat_is_preferred(postgres: mock.MagicMock) -> None:
    meter = DbCpuMeter()
    with schedstat("123456 7 8\n"):
        meter.attach()
        assert (meter.pid, meter.source, meter.read_ns()) == (42, "schedstat", 123456)
    postgres.ensure_connection.assert_called_once()


@pytest.mark.parametrize("content", [FileNotFoundError(), PermissionError(), "", "not-a-number 1", "0 0 0"])
def test_unusable_schedstat_falls_back_to_psutil(postgres: mock.MagicMock, content: str | Exception) -> None:
    meter = DbCpuMeter()
    with schedstat(content), process_times(user=1.5, system=0.5):
        meter.attach()
        assert (meter.source, meter.read_ns()) == ("psutil", 2_000_000_000)


def test_source_is_only_detected_again_for_a_new_backend(postgres: mock.MagicMock) -> None:
    meter = DbCpuMeter()
    with schedstat("5 0 0") as read:
        meter.attach()
        meter.attach()
        assert read.call_count == 1
        postgres.connection.get_backend_pid.return_value = 43  # reconnected
        meter.attach()
        assert (meter.pid, read.call_count) == (43, 2)


# ---- CpuMeasurement --------------------------------------------------------------------------------


@pytest.fixture
def clocks() -> Iterator[None]:
    with (
        mock.patch("carbon.cpu.time.perf_counter_ns", side_effect=[1_000, 11_000]),
        mock.patch("carbon.cpu.time.process_time_ns", side_effect=[500, 2_500]),
    ):
        yield


def fake_db_meter() -> mock.Mock:
    return mock.Mock(**{"read_ns.side_effect": [100, 400]})


def test_measurement_records_wall_python_and_database_time(clocks: None) -> None:
    db = fake_db_meter()
    with CpuMeasurement(db) as measurement:
        db.attach.assert_called_once()
    assert (measurement.wall_ns, measurement.python_cpu_ns, measurement.db_cpu_ns) == (10_000, 2_000, 300)
    assert measurement.cpu_s == pytest.approx(2_300 / 1e9)


def test_measurement_does_not_swallow_exceptions_and_still_records(clocks: None) -> None:
    measurement = CpuMeasurement(fake_db_meter())
    with pytest.raises(KeyError), measurement:
        raise KeyError("boom")
    assert measurement.db_cpu_ns == 300


# ---- EnergyMeterMiddleware -------------------------------------------------------------------------


@pytest.fixture
def make_middleware(settings, tmp_path: Path) -> Iterator[Callable[..., EnergyMeterMiddleware]]:
    settings.LOGS_DIR = tmp_path

    def _make(response: Callable = lambda request: HttpResponse(b"12345")) -> EnergyMeterMiddleware:
        return EnergyMeterMiddleware(response)

    # The test process must not keep a handler that writes the log when it exits.
    with mock.patch.object(meter_module.atexit, "register") as register:
        yield _make
    assert all(call.args[0].__name__ == "flush" for call in register.call_args_list)


def logged_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def request_for(view_name: str | None, method: str = "get"):
    request = getattr(RequestFactory(), method)("/secret-path/", REMOTE_ADDR="203.0.113.7")
    if view_name:
        request.resolver_match = mock.Mock(view_name=view_name)
    return request


def test_requests_are_summed_per_view_and_method(make_middleware, tmp_path: Path) -> None:
    middleware = make_middleware()
    with (
        mock.patch.object(meter_module.time, "thread_time_ns", side_effect=[0, 10, 10, 30, 30, 60]),
        mock.patch.object(DbCpuMeter, "read_ns", side_effect=[0, 1, 1, 3, 3, 6]),
    ):
        for method in ("get", "get", "post"):
            assert middleware(request_for("home", method)).content == b"12345"
    assert not (tmp_path / "energy_usage.jsonl").exists()  # nothing is written before the interval is over

    middleware.flush()

    by_method = {line["method"]: line for line in logged_lines(tmp_path / "energy_usage.jsonl")}
    get = by_method["GET"]
    assert (get["view"], get["requests"], get["python_cpu_ns"], get["db_cpu_ns"], get["bytes"]) == (
        "home",
        2,
        30,
        3,
        10,
    )
    assert (by_method["POST"]["requests"], by_method["POST"]["python_cpu_ns"]) == (1, 30)
    assert get["ts"]


def test_log_contains_no_personal_data(make_middleware, tmp_path: Path) -> None:
    middleware = make_middleware()
    middleware(request_for(None))
    middleware.flush()
    raw = (tmp_path / "energy_usage.jsonl").read_text()
    assert "203.0.113.7" not in raw and "secret-path" not in raw
    assert set(json.loads(raw)) == {"ts", "view", "method", "requests", "python_cpu_ns", "db_cpu_ns", "bytes"}
    assert json.loads(raw)["view"] == "unresolved"


def test_streaming_responses_count_no_bytes(make_middleware, tmp_path: Path) -> None:
    middleware = make_middleware(lambda request: StreamingHttpResponse(iter([b"chunk"])))
    middleware(request_for("stream"))
    middleware.flush()
    assert logged_lines(tmp_path / "energy_usage.jsonl")[0]["bytes"] == 0


def test_flush_appends_and_resets_the_sums(make_middleware, tmp_path: Path) -> None:
    middleware = make_middleware()
    middleware.flush()  # nothing recorded yet: no file
    assert not (tmp_path / "energy_usage.jsonl").exists()
    for _ in range(2):
        middleware(request_for("home"))
        middleware.flush()
    middleware.flush()
    assert [line["requests"] for line in logged_lines(tmp_path / "energy_usage.jsonl")] == [1, 1]


def test_log_is_written_automatically_after_the_interval(make_middleware, tmp_path: Path) -> None:
    middleware = make_middleware()
    middleware.last_flush -= FLUSH_SECONDS + 1
    middleware(request_for("home"))
    assert len(logged_lines(tmp_path / "energy_usage.jsonl")) == 1
    assert not middleware.stats


def test_every_thread_has_its_own_database_meter(make_middleware) -> None:
    middleware = make_middleware()
    own = middleware._meter()
    assert middleware._meter() is own
    others: list[DbCpuMeter] = []
    thread = threading.Thread(target=lambda: others.append(middleware._meter()))
    thread.start()
    thread.join()
    assert others[0] is not own
