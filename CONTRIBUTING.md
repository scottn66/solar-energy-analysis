# Contributing Guide

## Branching Strategy

- `main` - stable, reviewed code only
- `feature/<name>` - new features or notebooks
- `fix/<name>` - bug fixes

## Workflow

1. Pull latest from `main`
2. Create a feature branch: `git checkout -b feature/your-task`
3. Make changes, commit with clear messages
4. Push and open a Pull Request
5. Request review from at least one teammate
6. Merge after approval

## Commit Messages

Use clear, descriptive messages:
```
Add battery adoption trend chart to EDA
Fix ZIP code filtering for Peninsula region
Update pipeline to include Kaggle supplement
```

## Notebook Conventions

- Each notebook should be self-contained (import its own dependencies)
- Add markdown headers to separate logical sections
- Clear outputs before committing: `Cell > All Output > Clear`
- Keep data files out of git (they're in `.gitignore`)

## Sprint Cadence

- Sprints are 1 week long
- Check the GitHub Project board for current sprint tasks
- Update issue status as you work (To Do -> In Progress -> Done)
