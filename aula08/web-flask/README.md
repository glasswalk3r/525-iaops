# web-flask

Simples aplicação Flask para demonstração.

## Como construir e executar

1.  **Construir a imagem Docker:**
    ```sh
    docker build -t web-flask .
    ```

2.  **Executar o contêiner:**
    ```sh
    docker run -p 5000:5000 web-flask
    ```

Acesse a aplicação em `http://localhost:5000` e o health check em `http://localhost:5000/healthz`.
