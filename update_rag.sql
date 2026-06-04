-- Run this in SQLite to update existing UserProfile records:
UPDATE chat_userprofile SET rag_dataset_name='fin_reports', rag_top_k=10 WHERE rag_dataset_name='knowledge_base' OR rag_top_k=4;
SELECT user_id, rag_dataset_name, rag_top_k, rag_min_score FROM chat_userprofile;
