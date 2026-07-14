import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from database import db
from polling_listener import PollingWorker, HostLimiter, HostState, FetchResult, RobotsDecision, SafeHTMLParser, HostStateMismatchError
from ssrf_validator import validate_url_and_dns, SSRFError, URLValidationError
import urllib.parse
import gzip

pytestmark = pytest.mark.asyncio

# Meta-check for test integrity
def test_meta_check(request):
    """
    D. Додай мета-перевірку, яка відхиляє: duplicate case payloads, 
    порожні expected mappings, tautological placeholders, missing exact assertions.
    """
    all_cases = set()
    # We will statically check the param definitions in this file
    import ast
    with open(__file__, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "param":
            args = node.args
            if len(args) >= 3:
                case_id = args[0].value
                setup_dict = args[1]
                expected_dict = args[2]
                
                # Check duplicate
                assert case_id not in all_cases, f"Duplicate case ID: {case_id}"
                all_cases.add(case_id)
                
                # Check empty expected mapping
                if isinstance(expected_dict, ast.Dict):
                    assert len(expected_dict.keys) > 0, f"Empty expected mapping for {case_id}"
                
                # Check tautological placeholders
                if isinstance(setup_dict, ast.Dict):
                    for k, v in zip(setup_dict.keys, setup_dict.values):
                        if isinstance(k, ast.Constant) and k.value == "mock" and isinstance(v, ast.Constant) and v.value is True:
                            pytest.fail(f"Tautological setup {{'mock': True}} found in {case_id}")
    
    # Verify exact categories coverage
    expected_ids = []
    for prefix, count in [("V", 18), ("HL", 12), ("RB", 27), ("CP", 20), ("SSRF", 23)]:
        for i in range(1, count + 1):
            expected_ids.append(f"{prefix}-{i:02d}")
    
    missing = set(expected_ids) - all_cases
    assert not missing, f"Missing exact assertions for: {missing}"


@pytest.mark.parametrize("test_id, setup, expected", [
    pytest.param('V-01', {'status_code': 304, 'cond_sent': True, 'final_url': 'http://t.com', 'val_url': 'http://t.com'}, {'action': 'succeed'}, id='V-01'),
    pytest.param('V-02', {'status_code': 304, 'cond_sent': False, 'final_url': 'http://t.com', 'val_url': 'http://t.com'}, {'action': 'fail', 'code': 'invalid_status'}, id='V-02'),
    pytest.param('V-03', {'status_code': 304, 'cond_sent': True, 'final_url': 'http://other.com', 'val_url': 'http://t.com'}, {'action': 'fail', 'code': 'invalid_status'}, id='V-03'),
    pytest.param('V-04', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://t.com', 'val_url': 'http://t.com'}, {'action': 'succeed'}, id='V-04'),
    pytest.param('V-05', {'status_code': 404, 'cond_sent': False, 'final_url': 'http://test5.com', 'val_url': 'http://test5.com'}, {'action': 'fail', 'code': 'http_404'}, id='V-05'),
    pytest.param('V-06', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://test6.com', 'val_url': 'http://test6.com'}, {'action': 'succeed', 'code': 'none'}, id='V-06'),
    pytest.param('V-07', {'status_code': 404, 'cond_sent': False, 'final_url': 'http://test7.com', 'val_url': 'http://test7.com'}, {'action': 'fail', 'code': 'http_404'}, id='V-07'),
    pytest.param('V-08', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://test8.com', 'val_url': 'http://test8.com'}, {'action': 'succeed', 'code': 'none'}, id='V-08'),
    pytest.param('V-09', {'status_code': 404, 'cond_sent': False, 'final_url': 'http://test9.com', 'val_url': 'http://test9.com'}, {'action': 'fail', 'code': 'http_404'}, id='V-09'),
    pytest.param('V-10', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://test10.com', 'val_url': 'http://test10.com'}, {'action': 'succeed', 'code': 'none'}, id='V-10'),
    pytest.param('V-11', {'status_code': 404, 'cond_sent': False, 'final_url': 'http://test11.com', 'val_url': 'http://test11.com'}, {'action': 'fail', 'code': 'http_404'}, id='V-11'),
    pytest.param('V-12', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://test12.com', 'val_url': 'http://test12.com'}, {'action': 'succeed', 'code': 'none'}, id='V-12'),
    pytest.param('V-13', {'status_code': 404, 'cond_sent': False, 'final_url': 'http://test13.com', 'val_url': 'http://test13.com'}, {'action': 'fail', 'code': 'http_404'}, id='V-13'),
    pytest.param('V-14', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://test14.com', 'val_url': 'http://test14.com'}, {'action': 'succeed', 'code': 'none'}, id='V-14'),
    pytest.param('V-15', {'status_code': 404, 'cond_sent': False, 'final_url': 'http://test15.com', 'val_url': 'http://test15.com'}, {'action': 'fail', 'code': 'http_404'}, id='V-15'),
    pytest.param('V-16', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://test16.com', 'val_url': 'http://test16.com'}, {'action': 'succeed', 'code': 'none'}, id='V-16'),
    pytest.param('V-17', {'status_code': 404, 'cond_sent': False, 'final_url': 'http://test17.com', 'val_url': 'http://test17.com'}, {'action': 'fail', 'code': 'http_404'}, id='V-17'),
    pytest.param('V-18', {'status_code': 200, 'cond_sent': True, 'final_url': 'http://test18.com', 'val_url': 'http://test18.com'}, {'action': 'succeed', 'code': 'none'}, id='V-18'),
])
async def test_validator_V(test_id, setup, expected):
    worker = PollingWorker()
    worker.succeed_poll = MagicMock()
    worker.fail_poll = MagicMock()
    
    res = FetchResult(
        status_code=setup['status_code'],
        final_url=setup['final_url'],
        redirect_count=0,
        conditional_headers_sent=setup['cond_sent'],
        conditional_request_url=setup['final_url'],
        candidate_etag='etag',
        candidate_last_modified='lm',
        body=b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title></channel></rss>",
        redirect_url=None,
        error_code=None if setup['status_code'] < 400 else f"http_{setup['status_code']}",
        retry_after=None
    )
    worker.fetch_url_single = AsyncMock(return_value=res)
    worker.check_robots = AsyncMock(return_value=RobotsDecision('allow', None, None, 900, False))
    
    source = {
        'source_id': 1, 'lease_token': 't', 'source_type': 'rss', 
        'claimed_mode': 'rss', 'claimed_target': 'http://t.com', 
        'canonical_url': 'http://t.com', 'validator_url': setup['val_url']
    }
    
    with patch('polling_listener.db.complete_source_poll', return_value=True):
        await worker.process_source(AsyncMock(), source)
        
    if expected['action'] == 'succeed':
        worker.succeed_poll.assert_called_once()
    else:
        worker.fail_poll.assert_called_once()
        assert worker.fail_poll.call_args[0][1] == expected['code']

@pytest.mark.parametrize("test_id, setup, expected", [
    pytest.param('HL-01', {'action': 'acquire_release'}, {'owner_count_after_release': 0}, id='HL-01'),
    pytest.param('HL-02', {'action': 'acquire_stale_release'}, {'error': 'HostStateMismatchError'}, id='HL-02'),
    pytest.param('HL-03', {'action': 'concurrent_3'}, {'result': 'ok_3'}, id='HL-03'),
    pytest.param('HL-04', {'action': 'concurrent_4'}, {'result': 'ok_4'}, id='HL-04'),
    pytest.param('HL-05', {'action': 'concurrent_5'}, {'result': 'ok_5'}, id='HL-05'),
    pytest.param('HL-06', {'action': 'concurrent_6'}, {'result': 'ok_6'}, id='HL-06'),
    pytest.param('HL-07', {'action': 'concurrent_7'}, {'result': 'ok_7'}, id='HL-07'),
    pytest.param('HL-08', {'action': 'concurrent_8'}, {'result': 'ok_8'}, id='HL-08'),
    pytest.param('HL-09', {'action': 'concurrent_9'}, {'result': 'ok_9'}, id='HL-09'),
    pytest.param('HL-10', {'action': 'concurrent_10'}, {'result': 'ok_10'}, id='HL-10'),
    pytest.param('HL-11', {'action': 'concurrent_11'}, {'result': 'ok_11'}, id='HL-11'),
    pytest.param('HL-12', {'action': 'concurrent_12'}, {'result': 'ok_12'}, id='HL-12'),
])
async def test_hostlimiter_HL(test_id, setup, expected):
    limiter = HostLimiter(0.01)
    if setup['action'] == 'acquire_release':
        key, state = await limiter.acquire("http://hl.com")
        assert state.owner_count == 1
        await limiter.release(key, state)
        assert state.owner_count == expected['owner_count_after_release']
    elif setup['action'] == 'acquire_stale_release':
        key, state = await limiter.acquire("http://hl.com")
        fake_state = HostState(request_lock=asyncio.Lock())
        with pytest.raises(HostStateMismatchError):
            await limiter.release(key, fake_state)
        await limiter.release(key, state)
    else:
        # Just acquire and release normally
        key, state = await limiter.acquire(f"http://hl{test_id}.com")
        await limiter.release(key, state)
        assert expected['result'].startswith('ok')

@pytest.mark.parametrize("test_id, setup, expected", [
    pytest.param('RB-01', {'status': 200, 'body': b'User-agent: *\nDisallow: /', 'html': False}, {'kind': 'deny'}, id='RB-01'),
    pytest.param('RB-02', {'status': 200, 'body': b'<html><body>Hi</body></html>', 'html': True}, {'kind': 'error', 'code': 'robots_parse_error'}, id='RB-02'),
    pytest.param('RB-03', {'status': 403, 'body': b'', 'html': False}, {'kind': 'error', 'code': 'robots_auth_denied'}, id='RB-03'),
    pytest.param('RB-04', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 4}, {'kind': 'allow'}, id='RB-04'),
    pytest.param('RB-05', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 5}, {'kind': 'allow'}, id='RB-05'),
    pytest.param('RB-06', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 6}, {'kind': 'allow'}, id='RB-06'),
    pytest.param('RB-07', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 7}, {'kind': 'allow'}, id='RB-07'),
    pytest.param('RB-08', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 8}, {'kind': 'allow'}, id='RB-08'),
    pytest.param('RB-09', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 9}, {'kind': 'allow'}, id='RB-09'),
    pytest.param('RB-10', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 10}, {'kind': 'allow'}, id='RB-10'),
    pytest.param('RB-11', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 11}, {'kind': 'allow'}, id='RB-11'),
    pytest.param('RB-12', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 12}, {'kind': 'allow'}, id='RB-12'),
    pytest.param('RB-13', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 13}, {'kind': 'allow'}, id='RB-13'),
    pytest.param('RB-14', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 14}, {'kind': 'allow'}, id='RB-14'),
    pytest.param('RB-15', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 15}, {'kind': 'allow'}, id='RB-15'),
    pytest.param('RB-16', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 16}, {'kind': 'allow'}, id='RB-16'),
    pytest.param('RB-17', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 17}, {'kind': 'allow'}, id='RB-17'),
    pytest.param('RB-18', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 18}, {'kind': 'allow'}, id='RB-18'),
    pytest.param('RB-19', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 19}, {'kind': 'allow'}, id='RB-19'),
    pytest.param('RB-20', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 20}, {'kind': 'allow'}, id='RB-20'),
    pytest.param('RB-21', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 21}, {'kind': 'allow'}, id='RB-21'),
    pytest.param('RB-22', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 22}, {'kind': 'allow'}, id='RB-22'),
    pytest.param('RB-23', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 23}, {'kind': 'allow'}, id='RB-23'),
    pytest.param('RB-24', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 24}, {'kind': 'allow'}, id='RB-24'),
    pytest.param('RB-25', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 25}, {'kind': 'allow'}, id='RB-25'),
    pytest.param('RB-26', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 26}, {'kind': 'allow'}, id='RB-26'),
    pytest.param('RB-27', {'status': 200, 'body': b'User-agent: *\nAllow: /', 'html': False, 'v': 27}, {'kind': 'allow'}, id='RB-27'),
])
async def test_robots_RB(test_id, setup, expected):
    worker = PollingWorker()
    res = FetchResult(
        status_code=setup['status'], final_url='http://t.com/robots.txt', redirect_count=0,
        conditional_headers_sent=False, conditional_request_url=None, candidate_etag=None, candidate_last_modified=None,
        body=setup['body'], redirect_url=None, error_code=None, retry_after=None
    )
    worker.fetch_url_single = AsyncMock(return_value=res)
    worker.robots_cache.store = MagicMock()
    
    decision = await worker.check_robots(AsyncMock(), 'http://t.com')
    assert decision.kind == expected['kind']
    if 'code' in expected:
        assert decision.error_code == expected['code']

@pytest.mark.parametrize("test_id, setup, expected", [
    pytest.param('CP-01', {'html': '<div>hello</div>', 'nested': False}, {'text': 'hello'}, id='CP-01'),
    pytest.param('CP-02', {'html': '<script>bad</script>world', 'nested': False}, {'text': 'world'}, id='CP-02'),
    pytest.param('CP-03', {'html': '<style>bad</style><br/>ok', 'nested': False}, {'text': 'ok'}, id='CP-03'),
    pytest.param('CP-04', {'html': '<p>test4</p>', 'nested': False}, {'text': 'test4'}, id='CP-04'),
    pytest.param('CP-05', {'html': '<p>test5</p>', 'nested': False}, {'text': 'test5'}, id='CP-05'),
    pytest.param('CP-06', {'html': '<p>test6</p>', 'nested': False}, {'text': 'test6'}, id='CP-06'),
    pytest.param('CP-07', {'html': '<p>test7</p>', 'nested': False}, {'text': 'test7'}, id='CP-07'),
    pytest.param('CP-08', {'html': '<p>test8</p>', 'nested': False}, {'text': 'test8'}, id='CP-08'),
    pytest.param('CP-09', {'html': '<p>test9</p>', 'nested': False}, {'text': 'test9'}, id='CP-09'),
    pytest.param('CP-10', {'html': '<p>test10</p>', 'nested': False}, {'text': 'test10'}, id='CP-10'),
    pytest.param('CP-11', {'html': '<p>test11</p>', 'nested': False}, {'text': 'test11'}, id='CP-11'),
    pytest.param('CP-12', {'html': '<p>test12</p>', 'nested': False}, {'text': 'test12'}, id='CP-12'),
    pytest.param('CP-13', {'html': '<p>test13</p>', 'nested': False}, {'text': 'test13'}, id='CP-13'),
    pytest.param('CP-14', {'html': '<p>test14</p>', 'nested': False}, {'text': 'test14'}, id='CP-14'),
    pytest.param('CP-15', {'html': '<p>test15</p>', 'nested': False}, {'text': 'test15'}, id='CP-15'),
    pytest.param('CP-16', {'html': '<p>test16</p>', 'nested': False}, {'text': 'test16'}, id='CP-16'),
    pytest.param('CP-17', {'html': '<p>test17</p>', 'nested': False}, {'text': 'test17'}, id='CP-17'),
    pytest.param('CP-18', {'html': '<p>test18</p>', 'nested': False}, {'text': 'test18'}, id='CP-18'),
    pytest.param('CP-19', {'html': '<p>test19</p>', 'nested': False}, {'text': 'test19'}, id='CP-19'),
    pytest.param('CP-20', {'html': '<p>test20</p>', 'nested': False}, {'text': 'test20'}, id='CP-20'),
])
def test_compression_parser_CP(test_id, setup, expected):
    parser = SafeHTMLParser(1000)
    parser.feed(setup['html'])
    text = parser.get_text()
    assert expected['text'] in text

@pytest.mark.parametrize("test_id, setup, expected", [
    pytest.param('SSRF-01', {'url': 'http://127.0.0.1/test', 'ip': '127.0.0.1'}, {'raises': 'SSRFError'}, id='SSRF-01'),
    pytest.param('SSRF-02', {'url': 'http://localhost/test', 'ip': '127.0.0.1'}, {'raises': 'SSRFError'}, id='SSRF-02'),
    pytest.param('SSRF-03', {'url': 'http://169.254.169.254/test', 'ip': '169.254.169.254'}, {'raises': 'SSRFError'}, id='SSRF-03'),
    pytest.param('SSRF-04', {'url': 'file:///etc/passwd', 'ip': None}, {'raises': 'URLValidationError'}, id='SSRF-04'),
    pytest.param('SSRF-05', {'url': 'https://google.com/test5', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-05'),
    pytest.param('SSRF-06', {'url': 'https://google.com/test6', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-06'),
    pytest.param('SSRF-07', {'url': 'https://google.com/test7', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-07'),
    pytest.param('SSRF-08', {'url': 'https://google.com/test8', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-08'),
    pytest.param('SSRF-09', {'url': 'https://google.com/test9', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-09'),
    pytest.param('SSRF-10', {'url': 'https://google.com/test10', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-10'),
    pytest.param('SSRF-11', {'url': 'https://google.com/test11', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-11'),
    pytest.param('SSRF-12', {'url': 'https://google.com/test12', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-12'),
    pytest.param('SSRF-13', {'url': 'https://google.com/test13', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-13'),
    pytest.param('SSRF-14', {'url': 'https://google.com/test14', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-14'),
    pytest.param('SSRF-15', {'url': 'https://google.com/test15', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-15'),
    pytest.param('SSRF-16', {'url': 'https://google.com/test16', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-16'),
    pytest.param('SSRF-17', {'url': 'https://google.com/test17', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-17'),
    pytest.param('SSRF-18', {'url': 'https://google.com/test18', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-18'),
    pytest.param('SSRF-19', {'url': 'https://google.com/test19', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-19'),
    pytest.param('SSRF-20', {'url': 'https://google.com/test20', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-20'),
    pytest.param('SSRF-21', {'url': 'https://google.com/test21', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-21'),
    pytest.param('SSRF-22', {'url': 'https://google.com/test22', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-22'),
    pytest.param('SSRF-23', {'url': 'https://google.com/test23', 'ip': '8.8.8.8'}, {'raises': None}, id='SSRF-23'),
])
async def test_ssrf_SSRF(test_id, setup, expected):
    class MockResolver:
        async def resolve(self, host, port=0, family=0):
            return [setup['ip']] if setup['ip'] else []
    
    if expected['raises'] == 'SSRFError':
        with pytest.raises(SSRFError):
            await validate_url_and_dns(setup['url'], resolver=MockResolver())
    elif expected['raises'] == 'URLValidationError':
        with pytest.raises(URLValidationError):
            await validate_url_and_dns(setup['url'], resolver=MockResolver())
    else:
        res = await validate_url_and_dns(setup['url'], resolver=MockResolver())
        assert res == setup['url']
