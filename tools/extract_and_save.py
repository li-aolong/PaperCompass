#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import os
import sys
from pathlib import Path

# papercompass目录
ROOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
TOOLS_DIR = os.path.join(ROOT_DIR, "tools")

# 会议关键字段配置
CONFERENCE_KEY_FIELDS = {
    'aaai': ['track', 'status', 'primary_area'],
    'acl': ['track', 'status', 'award'],
    'acmmm': ['track', 'status', 'primary_area'],
    'aistats': ['track', 'status', 'primary_area'],
    'colm': ['track', 'status', 'primary_area'],
    'corl': ['track', 'status', 'primary_area'],
    'cvpr': ['track', 'status'],
    'eccv': ['track', 'status'],
    'emnlp': ['track', 'status', 'award'],
    'iccv': ['track', 'status', 'award', 'session'],
    'iclr': ['track', 'status', 'primary_area'],
    'icml': ['track', 'status', 'primary_area'],
    'ijcai': ['track', 'status', 'primary_area'],
    'nips': ['track', 'status', 'primary_area'],
    'siggraph': ['track', 'status', 'sess'],
    'siggraphasia': ['track', 'status', 'sess'],
    'wacv': ['track', 'status'],
    'www': ['track', 'status', 'primary_area']
}
    


# 设置控制台输出编码为UTF-8
sys.stdout.reconfigure(encoding='utf-8') if hasattr(sys.stdout, 'reconfigure') else None

def extract_unique_values(json_file_path, fields_to_extract=None):
    """
    从指定的JSON文件中提取指定字段的唯一值
    
    Args:
        json_file_path (str): JSON文件的路径
        fields_to_extract (list): 要提取的字段列表，默认为None（提取所有可用字段）
        
    Returns:
        dict: 包含各字段唯一值的字典
    """
    # 检查文件是否存在
    if not os.path.exists(json_file_path):
        print(f"错误: 文件 {json_file_path} 不存在")
        return None
    
    # 初始化结果字典，用于存储所有字段的唯一值
    unique_values = {}
    
    # 为每个要提取的字段初始化集合
    for field in fields_to_extract:
        unique_values[field] = set()
    
    try:
        # 打开并读取JSON文件
        with open(json_file_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
            
            # 确保数据是列表或字典
            if isinstance(data, list):
                # 如果是列表，遍历每个项目
                for item in data:
                    if isinstance(item, dict):
                        # 提取每个指定字段的值（如果存在）
                        for field in fields_to_extract:
                            if field in item and item[field] is not None:
                                # 对于布尔值，直接添加True/False
                                # if isinstance(item[field], bool):
                                #     unique_values[field].add(str(item[field]))
                                # else:
                                unique_values[field].add(item[field])
                                # if 'acl' in json_file_path and field == 'award':
                                #     print(unique_values[field])
                                #     print(isinstance(item[field], bool))
                                #     exit()
            elif isinstance(data, dict):
                # 如果是字典，检查每个键值对
                for key, value in data.items():
                    if isinstance(value, dict):
                        # 提取每个指定字段的值（如果存在）
                        for field in fields_to_extract:
                            if field in value and value[field] is not None:
                                # 对于布尔值，直接添加True/False
                                # if isinstance(value[field], bool):
                                #     unique_values[field].add(str(value[field]))
                                # else:
                                unique_values[field].add(value[field])
                                    
                                # if 'acl' in json_file_path:
                                #     print(unique_values[field])
                                #     exit()
            else:
                print(f"错误: JSON数据既不是列表也不是字典 - {json_file_path}")
                return None
    
    except json.JSONDecodeError:
        print(f"错误: 无法解析JSON文件 {json_file_path}")
        return None
    except Exception as e:
        print(f"错误: {str(e)} - {json_file_path}")
        return None
    
    # 将集合转换为列表并排序
    result = {}
    for field in fields_to_extract:
        result[field] = sorted(list(unique_values[field]))
    
    return result

def save_unique_values(unique_values, output_file_path):
    """
    将唯一值保存到JSON文件
    
    Args:
        unique_values (dict): 包含字段唯一值的字典
        output_file_path (str): 输出文件路径
    """
    # 确保输出目录存在
    os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
    
    # 保存到JSON文件
    with open(output_file_path, 'w', encoding='utf-8') as file:
        json.dump(unique_values, file, ensure_ascii=False, indent=4)
    
    print(f"已保存唯一值到: {output_file_path}")

def process_conference_files():
    """
    处理会议文件，提取唯一值并保存到对应的JSON文件
    """
    # 基础路径
    output_base_dir = os.path.join(TOOLS_DIR, "key_infos")
    
    # 处理每个会议
    for conf, fields in CONFERENCE_KEY_FIELDS.items():
        conf_dir = os.path.join(ROOT_DIR, conf)
        output_conf_dir = os.path.join(output_base_dir, conf)
        
        # 确保输出目录存在
        os.makedirs(output_conf_dir, exist_ok=True)
        
        if os.path.exists(conf_dir):
            print(f"\n处理 {conf.upper()} 会议文件...")
            
            # 遍历会议目录下的所有JSON文件
            for file in os.listdir(conf_dir):
                if file.endswith('.json'):
                    input_file_path = os.path.join(conf_dir, file)
                    output_file_path = os.path.join(output_conf_dir, file)
                    
                    print(f"正在处理: {file}")
                    
                    # 提取唯一值
                    unique_values = extract_unique_values(input_file_path, fields)
                    
                    if unique_values:
                        # 保存唯一值到JSON文件
                        save_unique_values(unique_values, output_file_path)
        else:
            print(f"警告: 会议目录不存在 - {conf_dir}")

def main():
    process_conference_files()

if __name__ == "__main__":
    main()
