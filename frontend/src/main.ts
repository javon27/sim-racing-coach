import { mount } from 'svelte'
import './app.css'
import App from './lib/App.svelte'
import { applyTheme, loadTheme } from './lib/theme'

// Apply the saved theme before first paint so the splash honors it (no flash).
applyTheme(loadTheme())

const app = mount(App, {
  target: document.getElementById('app')!,
})

export default app
