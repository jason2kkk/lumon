// 前端功能开关：可用 VITE_QUICK_SEARCH_ENABLED=0 隐藏雷达搜索。
export const QUICK_SEARCH_ENABLED = import.meta.env.DEV || import.meta.env.VITE_QUICK_SEARCH_ENABLED !== '0'
