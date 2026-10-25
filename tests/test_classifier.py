import unittest

from tests import helpers  # noqa: F401  (adds code/ to sys.path)
from ghostaudit.classifier import is_deletion_request

DELETION_REQUESTS = (
    "Please delete my account",
    "Please treat this message as a request for permanent account deletion.",
    "I'm deleting my account.",
    "Please erase everything",
    "right to be forgotten",
    "Purge my information",
    "Please close my account and process the associated personal-data deletion",
    "I closed my account last week",
    "Please confirm that the profile is gone",
    "Can you verify that my account was deleted? Please do not treat that record as mine.",
    "Solicito la eliminación permanente de la cuenta asociada a este correo.",
    "Solicito a exclusão permanente da conta associada a este e-mail.",
    "Je demande la suppression définitive du compte associé à cette adresse e-mail.",
    "このメールアドレスに関連するアカウントの完全な削除を依頼します。",
    "이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다.",
)

NON_DELETION_REQUESTS = (
    "",
    "Do not close the account",
    "Please don't delete my account",
    "Should not close this",
    "This is an information request, not a request to erase it.",
    "I still need the account, so I am not asking for deletion.",
    "Can you correct it without removing any of my records?",
    "Please close the support ticket",
    "Please disclose the information you hold",
    "Please send me my data",
    "Unsubscribe from marketing",
    "Export my data",
)


class ClassifierTest(unittest.TestCase):
    def test_deletion_requests(self):
        for text in DELETION_REQUESTS:
            with self.subTest(text=text):
                self.assertTrue(is_deletion_request(text))

    def test_non_deletion_requests(self):
        for text in NON_DELETION_REQUESTS:
            with self.subTest(text=text):
                self.assertFalse(is_deletion_request(text))

    def test_none_is_not_a_request(self):
        self.assertFalse(is_deletion_request(None))


if __name__ == "__main__":
    unittest.main()
