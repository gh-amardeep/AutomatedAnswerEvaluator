
import os
from openai import OpenAI


class ModelClient:
    """Configurable client for hosted or OpenAI-compatible inference."""

    def __init__(self):
        self.base_url = os.getenv(
            "MODEL_BASE_URL",
            "https://router.huggingface.co/v1",
        )

        self.model = os.getenv(
            "MODEL_NAME",
            "openai/gpt-oss-120b:groq",
        )

        api_key_env = os.getenv("MODEL_API_KEY_ENV", "HF_TOKEN")
        api_key = os.getenv(api_key_env)

        if not api_key:
            raise ValueError(
                f"Missing API key. Set {api_key_env} in your environment."
            )

        self.client = OpenAI(
            base_url=self.base_url,
            api_key=api_key,
        )

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.0,
    ) -> str:
        """Generate a response from the configured model."""

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": user_prompt,
                },
            ],
            temperature=temperature,
        )

        content = response.choices[0].message.content

        if not content:
            raise RuntimeError("The model returned an empty response.")

        return content.strip()
