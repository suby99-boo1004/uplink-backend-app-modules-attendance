# admin/users 500 해결 (User.deleted_at 없음)

## 에러
`AttributeError: type object 'User' has no attribute 'deleted_at'`

## 원인
`app/modules/admin/router.py`의 list_users에서 `User.deleted_at` 필터를 항상 적용.

## 최소 수정(추천)
`q = db.query(User)` 만든 뒤 아래를 추가:

```py
if hasattr(User, "deleted_at"):
    q = q.filter(User.deleted_at.is_(None))
```

그리고 기존의
```py
.filter(User.deleted_at.is_(None))
```
부분은 제거.

## 적용 파일
이 zip 안의 `router.py`는 위 로직이 반영된 예시입니다.
프로젝트의 `app/modules/admin/router.py`에 동일하게 반영하세요.
