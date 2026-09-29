# GeoLog — Plataforma de Telemetria Logística e Persistência Poliglota

**UNIPÊ** · Tópicos Avançados em Banco de Dados / Arquitetura de Software
**Professor:** Me. Ricardo Roberto de Lima
**Integrantes:** Vitor Jales Ramos Diniz e João Gabriel Lacerda

## Sobre o projeto

O GeoLog é um dashboard em Streamlit para a transportadora fictícia LogiTech Express. Ele aplica **persistência poliglota**, guardando cada tipo de dado no banco mais adequado:

| Banco | Papel | Conteúdo |
|-------|-------|----------|
| **SQLite** (`logitech.db`) | Dados cadastrais e transacionais, com integridade referencial | Tabelas `motoristas` e `veiculos` |
| **MongoDB** (`geolog_db.telemetria`) | Telemetria IoT com consultas geoespaciais | Documentos GeoJSON com temperatura, velocidade e `veiculo_id` |

Os dois bancos se encontram na aplicação, por meio do `veiculo_id` (join em memória com pandas).

## Funcionalidades

- **Persistência e seed (Módulo 1):** o esquema do SQLite e os dados de teste são criados automaticamente. No MongoDB, o índice `2dsphere` é criado por código na inicialização e o seed é carregado se a coleção estiver vazia.
- **Busca por raio (Módulo 2):** usa `$near` com `$maxDistance` sobre a última posição de cada veículo. O mapa Folium mostra o círculo do raio, o ponto de apoio, os veículos dentro do raio (verde ou vermelho, se estiver acima de 80 km/h), os de fora em cinza e a trilha do percurso.
- **Visão unificada (Módulo 3):** tabela com Nome do Motorista, Placa, Última Temperatura, Velocidade e Coordenadas, cruzando SQLite e MongoDB. Linhas com excesso de velocidade ficam destacadas.
- **Dashboard (Módulo 4):** KPIs (frotas ativas, temperatura média da carga, alertas de velocidade acima de 80 km/h) e gráficos Plotly (histórico de temperatura por veículo e status dos motoristas).
- **Bônus:** o botão **Simular Movimentação** insere novos pontos GPS com variação aleatória no MongoDB, e o mapa e o dashboard atualizam sem reiniciar o app. O botão **Resetar telemetria** restaura o seed.

## Requisitos

- Python 3.10 ou superior
- MongoDB em execução (local ou via Docker)

## Instalação e execução

1. Instale as dependências:

   ```bash
   pip install streamlit pymongo folium streamlit-folium plotly pandas
   ```

2. Suba o MongoDB. A forma mais simples é com Docker:

   ```bash
   docker run -d --name mongo-geolog -p 27017:27017 mongo:7
   ```

   Se preferir, use uma instalação local do MongoDB na porta padrão (27017).

3. Execute a aplicação:

   ```bash
   streamlit run geolog_app.py
   ```

4. Abra o endereço mostrado no terminal (normalmente http://localhost:8501).

### Configuração opcional

Para usar um MongoDB em outro endereço (por exemplo, MongoDB Atlas), defina a variável `MONGO_URI` antes de executar:

```bash
# Linux / macOS
export MONGO_URI="mongodb://usuario:senha@host:27017"

# Windows (PowerShell)
$env:MONGO_URI = "mongodb://usuario:senha@host:27017"
```

Se nada for definido, o padrão é `mongodb://localhost:27017`.

## Como usar

1. Na **barra lateral**, escolha um ponto de apoio (Centro, Cabo Branco, Tibiri/BR-230 ou coordenadas personalizadas) e ajuste o **raio de busca** em km.
2. Na aba **Mapa e Raio**, veja os veículos dentro do raio, ordenados por distância.
3. Na aba **Visão Unificada**, consulte os dados dos motoristas junto com a última telemetria.
4. Na aba **Dashboard**, acompanhe os KPIs e os gráficos.
5. Clique em **Simular Movimentação** algumas vezes para gerar histórico. Assim o gráfico de temperatura e a trilha no mapa ganham mais pontos.

## Estrutura dos dados

**SQLite**

```sql
motoristas (id PK, nome, cnh UNIQUE, status)
veiculos   (id PK, placa UNIQUE, modelo, motorista_id FK -> motoristas.id)
```

**MongoDB** (coleção `telemetria`)

```json
{
  "veiculo_id": 101,
  "location": { "type": "Point", "coordinates": [-34.873, -7.115] },
  "temperatura": 4.2,
  "velocidade": 65,
  "timestamp": "2026-09-11T10:00:00Z"
}
```

> O GeoJSON usa a ordem **[longitude, latitude]**. O Folium usa **[latitude, longitude]**, e o código faz essa conversão.

**Índices criados no MongoDB:** `location_2dsphere` (geoespacial) e `veiculo_ts` (`veiculo_id` + `timestamp` decrescente).

## Arquivos

| Arquivo | Descrição |
|---------|-----------|
| `geolog_app.py` | Aplicação Streamlit completa (entregável principal) |
| `relatorio_geolog.pdf` | Relatório técnico com o diagrama de arquitetura poliglota |
| `logitech.db` | Criado automaticamente na primeira execução |

## Solução de problemas

- **"Não foi possível conectar ao MongoDB":** confira se o servidor está rodando (`docker ps`) e se a porta 27017 está livre, ou ajuste `MONGO_URI`.
- **Mapa vazio ou nenhum veículo no raio:** aumente o raio na barra lateral ou escolha outro ponto de apoio.
- **Gráfico de temperatura com um ponto só:** use **Simular Movimentação** para gerar mais leituras.
- **Voltar ao estado inicial:** clique em **Resetar telemetria**. Para recriar também o SQLite, apague o arquivo `logitech.db`.

## Tecnologias

Python · Streamlit · PyMongo · SQLite3 · Folium · streamlit-folium · Plotly · pandas
