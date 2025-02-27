# Cluster Tools

[![CircleCI](https://circleci.com/gh/scalableminds/cluster_tools/tree/master.svg?style=svg)](https://circleci.com/gh/scalableminds/cluster_tools/tree/master)

This package provides python `Executor` classes for distributing tasks on a slurm cluster or via multi processing.


## Example

```python
import cluster_tools

def square(n):
  return n * n

if __name__ == '__main__':
  strategy = "slurm"  # other valid values are "multiprocessing" and "sequential"
  with cluster_tools.get_executor(strategy) as executor:
    result = list(executor.map(square, [2, 3, 4]))
    assert result == [4, 9, 16]
```

## Dev Setup

```
cd dockered-slurm
docker build -t slurm-docker-cluster:latest .
docker-compose up -d
docker exec -it slurmctld bash
docker exec -it c1 bash
```

Tests can be executed with `python3 -m pytest -s test.py` after entering the container.
Linting can be ran with `python3 -m pylint cluster_tools/`.
Code formatting (black) can be ran with `python3 -m black cluster_tools *.py`.

## Credits

Thanks to [sampsyo/clusterfutures](https://github.com/sampsyo/clusterfutures) for providing the slurm core abstraction and [giovtorres/slurm-docker-cluster](https://github.com/giovtorres/slurm-docker-cluster) for providing the slurm docker environment which we use for CI based testing.
