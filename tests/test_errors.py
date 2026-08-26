import unittest

from aiweekend_target.errors import ErrorCode, classify_upstream_status, local_response_status


class ErrorClassificationTests(unittest.TestCase):
    def test_upstream_auth_and_quota_aliases_keep_canonical_codes(self) -> None:
        self.assertEqual(classify_upstream_status(401), ErrorCode.AUTH)
        self.assertEqual(classify_upstream_status(403), ErrorCode.AUTH)
        self.assertEqual(classify_upstream_status(402), ErrorCode.QUOTA)
        self.assertEqual(classify_upstream_status(429), ErrorCode.QUOTA)

    def test_missing_model_and_other_upstream_statuses_keep_distinct_codes(self) -> None:
        self.assertEqual(classify_upstream_status(404), ErrorCode.MODEL_UNAVAILABLE)
        self.assertEqual(classify_upstream_status(500), ErrorCode.PROVIDER)

    def test_local_gateway_statuses_do_not_follow_upstream_aliases(self) -> None:
        self.assertEqual(local_response_status(ErrorCode.AUTH), 401)
        self.assertEqual(local_response_status(ErrorCode.QUOTA), 402)
        self.assertEqual(local_response_status(ErrorCode.MODEL_UNAVAILABLE), 404)
        self.assertEqual(local_response_status(ErrorCode.PROVIDER), 400)
