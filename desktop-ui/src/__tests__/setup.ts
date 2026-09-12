import '@testing-library/jest-dom'

// Node 26 暴露了一个未配置文件时不可用的实验性 localStorage，可能遮蔽 jsdom 实现。
// 测试使用内存存储，确保首次启动标记与真实 WebView 的 Storage 行为一致。
if (typeof window !== 'undefined' && (!window.localStorage || typeof window.localStorage.getItem !== 'function')) {
  const values = new Map<string, string>()
  const storage: Storage = {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key) },
    setItem: (key, value) => { values.set(key, String(value)) },
  }
  Object.defineProperty(window, 'localStorage', { configurable: true, value: storage })
}
