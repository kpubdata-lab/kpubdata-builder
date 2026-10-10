# infra

배포는 저장소 루트의 `docker-compose.prod.app.yml` 로 합니다. Docker Compose 가 도는 Linux VM
한 대에 Builder 를 올리고, `/data` 는 그 VM 의 로컬 볼륨(named volume `builder-data`)에
둡니다. 절차는 [`docs/deploy.md`](../docs/deploy.md) 에 있습니다.

| 경로 | 내용 |
| :--- | :--- |
| [`cubrid/`](./cubrid/) | VM 한 대에 Builder 와 CUBRID 를 함께 올리는 compose 구성 (ADR 0016) |

## Azure Container Apps 템플릿은 없앴습니다 (#1097)

이 디렉터리에는 Azure Container Apps + Azure Files 템플릿(`main.bicep`)이 있었습니다. 그
템플릿은 `/data` 를 Azure Files(네트워크 파일 공유)에 마운트했는데, Builder 의 상태는 `/data`
의 SQLite 파일이고 네트워크 파일시스템 위의 SQLite 는 파일 잠금이 불안정합니다(ADR 0010 §5,
`docs/deploy.md` 6절). 템플릿의 볼륨 종류만 바꿔서는 로컬 디스크가 되지 않습니다 — Container
Apps 의 다른 볼륨은 재시작하면 비워지는 임시 저장소입니다. 그래서 고치지 않고 없앴습니다.

배포 대상은 특정 클라우드를 전제하지 않는 VM 한 대입니다(kpubdata-lab/kpubdata#812). Azure
에 올린다면 VM 을 만들고 위 compose 를 그대로 씁니다.
