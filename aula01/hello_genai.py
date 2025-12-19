import os
import sys
from google import genai

# Obrigatório: chave do Google AI Studio
api_key = os.environ.get("GOOGLE_API_KEY")
if not api_key:
    print(
        "ERRO: variável de ambiente GOOGLE_API_KEY não encontrada.\n"
        "Exporte sua chave do AI Studio antes de rodar:\n\n"
        '  export GOOGLE_API_KEY="SUA_CHAVE_DO_AI_STUDIO"\n',
        file=sys.stderr
    )
    sys.exit(1)

# Usa a Gemini Developer API (AI Studio) via API key
client = genai.Client(api_key=api_key)

resp = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Responda 'ok-studio' e nada mais."
)

print((resp.text or "").strip())
