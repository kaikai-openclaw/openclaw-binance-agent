from src.skills.skill1_collect import Skill1Collect
from src.infra.state_store import StateStore
import json

def test():
    store = StateStore(db_path='data/test_state.db')
    with open('config/schemas/skill1_schema.json') as f:
        schema = json.load(f)
    
    skill = Skill1Collect(store, schema)
    result = skill.run()
    print(json.dumps(result, indent=2, ensure_ascii=False))

if __name__ == '__main__':
    test()
