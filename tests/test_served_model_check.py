import unittest

from scripts_evaluation.served_model_check import served_model_problem

VLLM_WITH_LORA = [
    {"id": "Qwen/Qwen3.5-9B", "root": "Qwen/Qwen3.5-9B", "parent": None},
    {
        "id": "Atom-Electron-1.3-9B",
        "root": "CrowtherLabs/Atom-Electron-1.3-9B",
        "parent": "Qwen/Qwen3.5-9B",
    },
]


class ServedModelCheckTest(unittest.TestCase):
    def test_adapter_name_is_accepted(self):
        self.assertIsNone(served_model_problem("Atom-Electron-1.3-9B", VLLM_WITH_LORA))

    def test_base_name_with_served_adapter_is_rejected(self):
        problem = served_model_problem("Qwen/Qwen3.5-9B", VLLM_WITH_LORA)
        self.assertIn("without the served LoRA adapter", problem)
        self.assertIn("Atom-Electron-1.3-9B", problem)

    def test_base_name_allowed_when_explicit(self):
        self.assertIsNone(
            served_model_problem(
                "Qwen/Qwen3.5-9B", VLLM_WITH_LORA, allow_base_with_lora=True
            )
        )

    def test_unserved_name_is_rejected_even_when_allowed(self):
        problem = served_model_problem(
            "Qwen/Qwen3.5-4B", VLLM_WITH_LORA, allow_base_with_lora=True
        )
        self.assertIn("is not served", problem)

    def test_standalone_model_without_adapters_is_accepted(self):
        served = [{"id": "CrowtherLabs/Qwythos-9B-Analyst", "parent": None}]
        self.assertIsNone(served_model_problem("CrowtherLabs/Qwythos-9B-Analyst", served))


if __name__ == "__main__":
    unittest.main()
