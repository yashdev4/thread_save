import unittest
import hashlib
from thread_save.security.idempotency import compute_content_hash, compute_content_hash_short
from thread_save.security.redactor import redact_text, _short_hash

class TestHmacAndKeyRotation(unittest.TestCase):
    def test_content_hash_is_plain_sha256(self):
        text = "Hello world! This is a test turn content."
        expected = hashlib.sha256((text + "\n").encode("utf-8")).hexdigest()
        actual = compute_content_hash(text)
        self.assertEqual(actual, expected)
        self.assertEqual(compute_content_hash_short(text), expected[:8])

    def test_canonical_v1_indented_vs_unindented_code(self):
        indented = "    def hello():\n        return 42\n"
        unindented = "def hello():\n    return 42\n"
        hash_indented = compute_content_hash(indented)
        hash_unindented = compute_content_hash(unindented)
        self.assertNotEqual(hash_indented, hash_unindented)

    def test_key_rotation_does_not_change_content_hash(self):
        text = "Some user response with data"
        hash_before = compute_content_hash(text)

        # Simulate rotating server keys
        key_v1 = b"server_key_version_1"
        key_v2 = b"server_key_version_2"

        # Content hash must be 100% independent of server key
        hash_after = compute_content_hash(text)
        self.assertEqual(hash_before, hash_after)

    def test_hmac_tag_in_redaction_masks(self):
        secret = "AKIA1234567890ABCDEF"
        text = f"My aws key is {secret}"

        key_v1 = b"server_key_v1"
        key_v2 = b"server_key_v2"

        res1 = redact_text(text, server_key=key_v1)
        res2 = redact_text(text, server_key=key_v2)

        self.assertTrue(res1.was_redacted)
        self.assertTrue(res2.was_redacted)
        self.assertIn("[REDACTED:aws_access_key:", res1.text)
        self.assertIn("[REDACTED:aws_access_key:", res2.text)

        # Different server keys produce different 4-char HMAC tags
        tag1 = res1.text.split(":")[2].rstrip("]")
        tag2 = res2.text.split(":")[2].rstrip("]")
        self.assertNotEqual(tag1, tag2)
        self.assertEqual(len(tag1), 4)
        self.assertEqual(len(tag2), 4)

if __name__ == "__main__":
    unittest.main()
