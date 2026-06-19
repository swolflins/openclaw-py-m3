---
name: weather
version: 0.1.0
description: 查询天气(模拟实现,不真调 API,演示 skill + tool + prompt 注入)
triggers: [天气, weather, 下雨, 气温]
requires_tools: [http_get]
---

# Weather Skill

当用户问天气时,优先用 `weather_query` 工具拿数据,再总结。
如果用户没指定城市,默认问"你指的是哪个城市?"。
