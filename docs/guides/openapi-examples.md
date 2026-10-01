# OpenAPI 예제 추출

`contract/builder-api.yaml`의 각 JSON media type에는 Studio와 사람이 함께 사용할 수 있는
named request/response example이 있다. 예제는 API 계약 `1.16.0`의 wire schema를 따르며,
credential 원문, 실제 secret, 내부 절대 경로를 포함하지 않는다.

Studio 같은 소비자는 저장소 루트에서 다음 명령으로 예제를 단일 JSON 문서로 추출할 수 있다.

```bash
uv run python scripts/extract_openapi_examples.py > openapi-examples.json
```

출력의 `examples` 배열은 `path`, `method`, `location`(`request` 또는
`response:<status>`), `media_type`, `name`, `summary`, `value`를 제공한다. 배열 순서는
OpenAPI 문서 순서와 같고 local `$ref` response/example도 해석한다. 생성 파일을 정본으로
커밋하지 말고 `contract/builder-api.yaml`에서 필요할 때 다시 추출한다.

## 응답 fixture (#814)

클라이언트가 "모르는 선택 필드는 받아들이고, 필수 필드 타입 오류는 거부하는지"를 검사할 수
있도록, 2xx response example 로부터 `contract/fixtures/responses.json` 을 생성해 커밋한다.
위 추출 결과와 달리 이 파일은 저장소에 두며, 테스트가 계약과의 drift 를 막는다.
non-2xx named example 은 `error_fixtures` 에 따로 담긴다(#947, #951) — 공유 응답은 한 번만.

```bash
uv run python scripts/generate_response_fixtures.py          # 다시 생성
uv run python scripts/generate_response_fixtures.py --check  # 오래되면 exit 1
```

형식과 규칙은 [API_CONTRACT.md](../API_CONTRACT.md#클라이언트-호환-규칙-814)를 따른다.
